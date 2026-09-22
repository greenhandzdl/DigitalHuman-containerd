#!/usr/bin/env python3
"""Fay 对话协议适配器（新增组件，不属于任何上游仓库，也不改动它们）。

存在的理由：后端 service 调的是 `POST {FAY_HTTP_URL}/api/chat`
（app/services/fay_gateway.py:38-52），而 Fay 的 Flask 5000 根本没有这个路由
——它只有异步的 `/api/send`（投递即返回 {"result":"successful"}）和
`/api/get-msg`（按用户名分页拉历史）。两边契约不同形，且 `/api/send` 不回传回复文本。

本适配器把「一问一答」补在中间：

    backend ──POST /api/chat──▶ adapter ──POST /api/send──▶ Fay
                                  │
                                  └──轮询 POST /api/get-msg 直到出现新的 type='fay' 行

要点：
* Fay 的回答有两层增长：一句话新增一行（core/fay_core.py:1229
  `add_content('fay','speak',text,...)`），同一流式行又会被反复覆写
  （core/fay_core.py:1336-1339 `accumulated_text = existing_content[3] + text`
  → `content_db.update_content`）。所以「有没有新行」不足以判断说完。
* **收工判据用的是哨兵，不是静默。** 上游的 `_<isfirst>` / `_<isend>` 标记在
  core/stream_manager.py:332-335 就被 replace 掉了，进不了 T_Msg.content；而单模型模式
  （llm/nlp_cognitive_stream.py `_is_single_model_mode()`）的工具循环是同步跑的，
  `/api/execution-status` 全程 idle —— 容器外面原本没有任何信号能区分「两阶段协议正在
  执行工具」和「这句就是最后一句」。只靠静默会在占位句写完、工具还在跑的那 3~30 秒里
  误判成说完，返回「占位句 + 半句」。patches/fay/0008 补了这个哨兵（`<dh-end>`，
  写在这一轮回答的正文末尾），看到它就立刻收工；
  没看到（镜像没按新补丁重建）才退回静默判定。
* 库里那一行是**原文**，不是给用户看的文本。core/fay_core.py:1410-1437 的 think 剥离
  只作用于发给数字人/WS 的那份，`/api/get-msg` 返回的 `content` 里留着
  `<think>…</think>`、`<prestart>…</prestart>`（prestart 工具结果）和占位句
  「我来帮你查一下，稍等…」（llm/nlp_cognitive_stream.py 的 `_on_tool_detected` /
  `_submit_tool_execution`）。所以返回前必须过一遍 `_clean()`。
* 用户名即 Fay 的会话/记忆主键（core/fay_core.py:836 首次交互自动建 member），
  默认 `elder_<user_id>`，从而复用 Fay 的 isolate_by_user 能力。
* 只用标准库，镜像即 python:3.12-slim，不引入任何依赖。

环境变量：
  FAY_BASE_URL              默认 http://fay:5000
  ADAPTER_PORT              默认 8010
  ADAPTER_MAX_WAIT_SECONDS  默认 60；compose 注入 500。整条链自下而上要单调变长：
                            LLM 420 < Fay 空闲上限 480 <= adapter 500 < 后端
                            FAY_FORWARD_TIMEOUT_SECONDS 520，反了会把 Fay 的正常
                            慢响应报成后端自己的错。理由见 containerd/.env.example。
  ADAPTER_POLL_INTERVAL     默认 0.6
  ADAPTER_SETTLE_SECONDS    默认 8.0 —— **只在没看到哨兵、且此刻手里已经有正文可交时**才用
                            （未重建镜像的退化路径）。两阶段协议里「占位句 → 工具执行 →
                            正式回答」这段静默实测 3~30s，2s 必被截成半句，所以这个兜底值
                            不能再当"响应速度"调；而只有占位句时收工交出去的一定是错的，
                            那种情况一直等到 MAX_WAIT（真链路实测：正文有 17.8s 才来的）。
  ADAPTER_USERNAME_PREFIX   默认 elder_
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAY_BASE_URL = os.environ.get("FAY_BASE_URL", "http://fay:5000").rstrip("/")
PORT = int(os.environ.get("ADAPTER_PORT", "8010"))
MAX_WAIT = float(os.environ.get("ADAPTER_MAX_WAIT_SECONDS", "60"))
POLL_INTERVAL = float(os.environ.get("ADAPTER_POLL_INTERVAL", "0.6"))
SETTLE = float(os.environ.get("ADAPTER_SETTLE_SECONDS", "8.0"))
USERNAME_PREFIX = os.environ.get("ADAPTER_USERNAME_PREFIX", "elder_")

HTTP_TIMEOUT = 8.0

# patches/fay/0008 落在正文末尾的结束哨兵。
END_SENTINEL = "<dh-end>"
# 上游在两阶段协议的执行期前推送的过渡语，会和正式回答写进同一行（见模块 docstring）。
FILLER_SENTENCES = ("我来帮你查一下，稍等…",)

# 正则照抄上游自己清洗历史时用的那两条（llm/nlp_cognitive_stream.py
# 的 _remove_prestart_from_text / _remove_think_from_text），不另立一套语义。
_PRESTART_RE = re.compile(r"<prestart[^>]*>[\s\S]*?</prestart>", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _PRESTART_RE.sub("", text)
    text = _THINK_RE.sub("", text)
    text = _THINK_TAG_RE.sub("", text)
    text = text.replace(END_SENTINEL, "")
    stripped = text.lstrip()
    for filler in FILLER_SENTENCES:
        if stripped.startswith(filler):
            stripped = stripped[len(filler):].lstrip()
            break
    return stripped.strip()


def _post_form(path: str, payload: dict) -> dict:
    """以 x-www-form-urlencoded 的 data 字段调用 Fay 接口（两个接口都读 form）。"""
    url = f"{FAY_BASE_URL}{path}"
    body = urllib.parse.urlencode({"data": json.dumps(payload, ensure_ascii=False)}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError(f"{path} 返回非 JSON: {raw[:120]!r}")


def _fetch_messages(username: str, limit: int = 30) -> list[dict]:
    res = _post_form("/api/get-msg", {"username": username, "limit": limit, "offset": 0})
    return res.get("list") or []


def _reply_rows(rows: list[dict], after_id: int) -> list[tuple[int, str]]:
    out = []
    for row in rows:
        if row.get("type") != "fay":
            continue
        try:
            rid = int(row.get("id", 0))
        except (TypeError, ValueError):
            continue
        text = (row.get("content") or "").strip()
        if rid > after_id and text:
            out.append((rid, text))
    out.sort(key=lambda item: item[0])
    return out


def _baseline_id(rows: list[dict]) -> int:
    ids = []
    for row in rows:
        try:
            ids.append(int(row.get("id", 0)))
        except (TypeError, ValueError):
            continue
    return max(ids) if ids else 0


def answer(username: str, text: str) -> tuple[str | None, str | None]:
    """投递问题并取回完整回答。返回 (content, error)。"""
    try:
        baseline = _baseline_id(_fetch_messages(username))
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        return None, f"Fay 历史读取失败: {exc}"

    try:
        _post_form("/api/send", {"username": username, "msg": text})
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        return None, f"Fay 投递失败: {exc}"

    deadline = time.monotonic() + MAX_WAIT
    collected: dict[int, str] = {}
    signature: dict[int, int] = {}
    last_growth = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            rows = _fetch_messages(username)
        except (urllib.error.URLError, RuntimeError, TimeoutError):
            continue  # 单轮轮询失败不值得整体放弃
        fresh = dict(_reply_rows(rows, baseline))
        # 行数变多 或 既有行被流式写长，都算"还在说话"
        current = {rid: len(text) for rid, text in fresh.items()}
        if current != signature:
            collected = fresh
            signature = current
            last_growth = time.monotonic()
        if any(END_SENTINEL in text for text in fresh.values()):
            break  # 哨兵：这一轮说完了。真信号，不再猜静默
        if collected and time.monotonic() - last_growth >= SETTLE:
            # 静默退路还要求「此刻手里已经有正文可交」。只剩占位句 / prestart 时交出去
            # 也是错的，而继续等没有代价 —— 上界由 MAX_WAIT 兜着。真回答可以迟到很久：
            # 远端 26B 实测有一轮 17.8s 才把整段正文写完，若按「有行就算说过话」在这里
            # break，那一轮就成了「本轮没有给出正文」。
            if _clean("".join(collected[k] for k in sorted(collected))):
                break  # 只有镜像没按 patches/fay/0008 重建时才会走到这条退路
    if not collected:
        return None, f"等待 Fay 回答超时（{MAX_WAIT:.0f}s，用户名 {username}）"
    joined = _clean("".join(collected[k] for k in sorted(collected)))
    if not joined:
        # 只剩占位句 / think / prestart，没有可给用户的话。回错误而不是回一段噪声，
        # 让 service 的 ok=False 分支（app/services/fay_gateway.py:57-63）接管。
        return None, f"Fay 本轮没有给出正文（用户名 {username}）"
    return joined, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名约定
        if self.path in ("/healthz", "/health"):
            self._json(HTTPStatus.OK, {"status": "ok", "fay_base_url": FAY_BASE_URL})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            self._json(HTTPStatus.NOT_FOUND, {"error": "only POST /api/chat is supported"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
            return

        content = (payload.get("content") or "").strip()
        if not content:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "content is required"})
            return

        username = payload.get("username") or f"{USERNAME_PREFIX}{payload.get('user_id', 'anon')}"
        started = time.monotonic()
        reply, error = answer(username, content)
        elapsed = time.monotonic() - started
        if error:
            # 让后端的 ok=False 分支生效（fay_gateway.py:57-63 会把 >=400 记为失败）
            self._json(HTTPStatus.BAD_GATEWAY, {"error": error, "elapsed_seconds": round(elapsed, 2)})
            return
        ref = f"{username}@{int(started)}"
        print(f"[adapter] user={username} {elapsed:.1f}s reply={len(reply)}chars", flush=True)
        self._json(HTTPStatus.OK, {"content": reply, "fay_session_ref": ref})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[adapter] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"[adapter] listening :{PORT} -> {FAY_BASE_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
