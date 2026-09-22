#!/usr/bin/env python3
"""adapter/server.py 的契约测试（新增测试件，不依赖显存、不依赖 Fay 容器）。

为什么单独要这一份：adapter 是 backend 和 Fay 之间唯一新增的一跳，而它此前只有
`./run.sh smoke` 一条端到端证据 —— 那条走的是 dh-fay 的 9b，本机显存被别人占满时
一发要 172~301s，跑不跑得动取决于这台机器此刻忙不忙。adapter 自己的逻辑
（轮询 + 流式覆写判停 + 用户名映射 + 502 语义）跟 LLM 快慢无关，就不该等显存。

所以这里用一个假 Fay 把 `service/app/services/fay_gateway.py` 依赖的那两个接口
按真实语义实现出来，逐条判：

  1. 一问一答：拿到完整回答 + `fay_session_ref` 形如 `<用户名>@<秒>`
  2. 流式覆写：同一行 content 被反复写长（core/fay_core.py:1336-1339 的真实行为），
     必须等它停下再拼，不能拿到半句就返回
  3. 多行：一句话分多行落库时按 id 升序拼接
  4. 只算新行：问答前已存在的 type='fay' 行不能混进回答（`_baseline_id` 的语义）
  5. 忽略非 fay 行：用户自己那行 type='user' 的问题不能当回答返回
  6. 轮询抖动：中途某次 /api/get-msg 抛 500 不能整体放弃
  7. 上游不说话：到 `ADAPTER_MAX_WAIT_SECONDS` 返回 502，错误文案带上限时和用户名
  8. Fay 不可达：连历史都读不到时也是 502，且文案区分「历史读取失败」
  9. 入参：空 content / 非法 JSON → 400；未知路径 → 404；GET /healthz → 200
 10. 用户名：显式 `username` 优先，否则 `ADAPTER_USERNAME_PREFIX + user_id`
 11. 两阶段协议 + 哨兵（patches/fay/0008）：占位句写完、工具还在跑的 3 秒静默里
     不能收工；看到 `<dh-end>` 立刻收工，且返回的是**洗完的正文**
 12. 没有哨兵（镜像没按 0008 重建）时仍按静默退路收工，但清洗照做
 13. 整轮只有占位句和思考内容 → 502 且文案是「没有给出正文」，不是把噪声回给用户
 14. 反向对照：把哨兵判定改坏，判据 11 的耗时那条必须变红（见 negative_control()）
 15. 正文迟到过整个静默窗口 → 继续等，不在「手里还没有正文」时收工交空

只用标准库，和 adapter 一样：`python:3.12-slim` 里直接跑得起来。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# ADAPTER_PATH 只为一件事：把被测对象换成一份「故意改坏」的副本，验证这份测试真的
# 能判出红（一份 11/11 全绿却杀不掉任何 bug 的测试，比没有测试更糟）。
ADAPTER = os.environ.get("ADAPTER_PATH") or os.path.normpath(
    os.path.join(HERE, "..", "adapter", "server.py"))

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


# --------------------------------------------------------------------------- 假 Fay
class FakeFay:
    """实现 /api/send + /api/get-msg，行为由每个场景注入的 script 决定。

    script(sent) -> [(时刻秒, [(type, content), ...]), ...]：在 answer 生成完之后
    按给定时刻把行「长出来」，模拟 Fay 的 content_db 反复覆写同一行。
    """

    def __init__(self, port: int, script, flaky: int = 0):
        self.port = port
        self.script = script
        self.flaky = flaky          # 基线之后前 flaky 次 /api/get-msg 直接抛 500
        self.reads = 0              # 第 0 次读是 adapter 取基线，不许失败
        self.rows: list[dict] = []  # 全量历史，id 单调递增
        self.added: list[int] = []  # 本轮脚本加过的行 id，供 grow 用负下标引用
        self.next_id = 1
        self.sent: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _data(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
                return json.loads((form.get("data") or ["{}"])[0])

            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                if self.path == "/api/send":
                    outer.on_send(self._data())
                    self._json(200, {"result": "successful"})
                elif self.path == "/api/get-msg":
                    if outer.take_flaky():
                        self._json(500, {"error": "fake hiccup"})
                    else:
                        req = self._data()
                        with outer.lock:
                            outer.reads += 1
                            rows = list(outer.rows)[-int(req.get("limit") or 30):]
                        self._json(200, {"list": rows})
                else:
                    self._json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, type_: str, content: str) -> int:
        with self.lock:
            row = {"id": self.next_id, "type": type_, "content": content}
            self.rows.append(row)
            self.next_id += 1
            self.added.append(row["id"])
            return row["id"]

    def grow(self, which: int, content: str) -> None:
        """按「脚本里第 which 个新增行」覆写 content —— Fay 流式回答就是这么长回去的。

        用相对下标而不是写死 id：假 Fay 在 /api/send 时会先落一行 type='user'
        （真实行为，也是判据 5 要验的），写死 id 会错一位并把用户的问题当成回答。
        """
        with self.lock:
            row_id = self.added[which]
            for row in self.rows:
                if row["id"] == row_id:
                    row["content"] = content
                    return
            raise AssertionError(f"假 Fay 里没有 id={row_id} 的行")

    def take_flaky(self) -> bool:
        with self.lock:
            # reads == 0 说明这是 adapter 取基线的那一发，失败会让它直接走
            # 「历史读取失败」分支，那样判据 6 量的就不是轮询抖动而是另一件事了。
            if self.reads == 0 or self.flaky <= 0:
                return False
            self.flaky -= 1
            return True

    def on_send(self, payload: dict) -> None:
        self.sent.append(payload)
        self.add("user", payload.get("msg", ""))
        self.added.clear()  # grow 的下标只指本轮脚本加的行
        script = self.script(payload)
        if not script:
            return

        def play():
            base = time.monotonic()
            for when, ops in script:
                time.sleep(max(0.0, base + when - time.monotonic()))
                for kind, arg in ops:
                    if kind == "add":
                        self.add(arg[0], arg[1])
                    elif kind == "grow":
                        self.grow(arg[0], arg[1])
            # 让最后一次覆写落定，避免测试自己制造的竞态
            time.sleep(0.05)

        threading.Thread(target=play, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ----------------------------------------------------------------------- adapter 起停
class Adapter:
    def __init__(self, fay_port: int, adapter_path: str | None = None, **env):
        self.port = 18100 + int(time.monotonic() * 1000) % 900
        base = {
            "FAY_BASE_URL": f"http://127.0.0.1:{fay_port}",
            "ADAPTER_PORT": str(self.port),
            "ADAPTER_POLL_INTERVAL": "0.05",
            "ADAPTER_SETTLE_SECONDS": "0.2",
            "ADAPTER_MAX_WAIT_SECONDS": "5",
        }
        base.update({k.upper(): str(v) for k, v in env.items()})
        merged = dict(os.environ, **base)
        self.proc = subprocess.Popen([sys.executable, adapter_path or ADAPTER], env=merged,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
        self.log = ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                out = self.get("/healthz")
                if json.loads(out).get("status") == "ok":
                    return
            except Exception:
                time.sleep(0.1)
        raise AssertionError(f"adapter 没起来：{self.proc.poll()}")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str) -> str:
        with urllib.request.urlopen(self.url(path), timeout=5) as resp:
            return resp.read().decode()

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(self.url(path), data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw}

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.log = self.proc.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.log = self.proc.communicate()[0]


# ------------------------------------------------------------------------------- 场景
# 两阶段协议在库里长什么样（core/fay_core.py:1181-1233 只在 is_first 时新建行，
# 续写一律 update_content 覆写同一行），所以这些片段全部落在**同一行**上。
GAP = 3.0                     # 占位句写完到正式回答写出之间的那段静默
ACK = "我来帮你查一下，稍等…\n"
NOISE = (ACK + "<prestart>血压参考值：收缩压 90~139 mmHg，舒张压 60~89 mmHg。</prestart>\n"
             "<think>\n执行耗时: 2.9s，共 1 步\n命中知识库片段 3 条\n</think>\n")
BODY = "老人测血压，收缩压在 90 到 139 之间算正常。"
SENTINEL = "\n<dh-end>"


def two_phase(with_sentinel: bool = True) -> list:
    ops = [(0.0, [("add", ("fay", ACK))]),
           (GAP, [("grow", (-1, NOISE + BODY))])]
    if with_sentinel:
        ops.append((GAP + 0.3, [("grow", (-1, NOISE + BODY + SENTINEL))]))
    return ops


def run_two_phase(port: int, script, settle: float, max_wait: float,
                  adapter_path: str | None = None) -> tuple[int, dict, float]:
    """起一套「假 Fay + adapter」跑两阶段脚本，返回 (HTTP 码, 回包, 整轮耗时)。

    耗时是判据 11/14 的**主断言对象**：哨兵该收工的时刻是 GAP+0.3，静默退路的收工
    时刻是「最后一次增长 + settle」，两者差好几秒，只有把时间量出来才分得清是哪条
    路生效了 —— 只看内容的话两条路都是绿的。
    """
    fake = FakeFay(port, lambda sent: script)
    ad = Adapter(port, adapter_path, adapter_settle_seconds=settle,
                 adapter_max_wait_seconds=max_wait, adapter_poll_interval=0.2)
    started = time.monotonic()
    try:
        code, body = ad.post("/api/chat", {"content": "血压正常值是多少？", "username": "elder_11"})
    finally:
        took = time.monotonic() - started
        ad.close()
        fake.stop()
    return code, body, took


def negative_control(port: int) -> None:
    """把哨兵判定改坏，判据 14 必须变红。

    一份杀不掉任何 bug 的测试比没有测试更糟：这里不是"再测一遍"，而是**证明**
    「哨兵一到就收工」这条判据真的挂在哨兵上。锚点匹配不上就直接判 FAIL ——
    那说明这份自检已经和实现漂走了，绿也没有意义（照 probes/asr_test.py 的写法）。
    """
    anchor = ("        if any(END_SENTINEL in text for text in fresh.values()):\n"
              "            break")
    src = open(ADAPTER, encoding="utf-8").read()
    if anchor not in src:
        check(False, "14 反向对照：改坏哨兵判定后「一到就收工」变红",
              "这份自检已不可信：adapter 里找不到哨兵判定的锚点")
        return
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, "server_broken.py")
        with open(broken, "w", encoding="utf-8") as f:
            f.write(src.replace(anchor, "        if False:  # 反向对照：拔掉哨兵\n            break", 1))
        # settle=8 让退路比哨兵晚得多，改坏之后必须明显更慢（照 11 的同一个脚本）
        code, body, took = run_two_phase(port, two_phase(), 8, 25, adapter_path=broken)
        check(code == 200 and body.get("content") == BODY and took > GAP + 2.0,
              "14 反向对照：改坏哨兵判定后「一到就收工」变红",
              f"改坏后 {took:.1f}s 才收工（哨兵在位时 {GAP + 0.3:.1f}s 出头）")


def main() -> int:
    port = 19001

    # 1/2/3/9/10 各自起一套「假 Fay + adapter」，互不串状态
    fake = FakeFay(port, lambda sent: [
        (0.0, [("add", ("fay", "你"))]),
        (0.1, [("grow", (-1, "你好，"))]),
        (0.2, [("grow", (-1, "你好，我是"))]),
        (0.3, [("grow", (-1, "你好，我是数字人"))]),
    ])
    ad = Adapter(port)
    code, body = ad.post("/api/chat", {"content": "在吗", "user_id": 7})
    check(code == 200 and body.get("content") == "你好，我是数字人",
          "1 一问一答：取回完整回答", f"HTTP {code} content={body.get('content')!r}")
    ref = str(body.get("fay_session_ref") or "")
    check(ref.startswith("elder_7@") and ref.split("@")[-1].isdigit(),
          "1b 用户名默认 elder_<user_id> 且回传 session ref", ref)
    ad.close(); fake.stop()

    fake = FakeFay(port, lambda sent: [
        (0.0, [("add", ("fay", "第一段。"))]),
        (0.1, [("add", ("fay", "第二段。"))]),
        (0.2, [("grow", (-1, "第二段。收尾了。"))]),
    ])
    ad = Adapter(port)
    code, body = ad.post("/api/chat", {"content": "多说两句", "username": "zhang_3"})
    check(code == 200 and body.get("content") == "第一段。第二段。收尾了。",
          "3 多行回答按 id 升序拼接", f"HTTP {code} content={body.get('content')!r}")
    check(str(body.get("fay_session_ref", "")).startswith("zhang_3@"),
          "10 显式 username 优先于前缀", str(body.get("fay_session_ref")))
    code, body = ad.post("/api/chat", {"content": "", "username": "zhang_3"})
    check(code == 400, "9a 空 content → 400", f"HTTP {code}")
    code, body = ad.post("/nope", {"content": "x"})
    check(code == 404, "9b 未知路径 → 404", f"HTTP {code}")
    ad.close(); fake.stop()

    # 4 只算新行：历史里已有一行 fay，不能把它当成本轮回答
    pre = FakeFay(port, lambda sent: [(0.0, [("add", ("fay", "本轮真回答。"))])])
    pre.add("fay", "这是上一轮遗留的回答，不该被拼进来。")
    ad = Adapter(port)
    code, body = ad.post("/api/chat", {"content": "再来", "username": "elder_9"})
    check(code == 200 and body.get("content") == "本轮真回答。",
          "4 只取基线之后的新行", f"HTTP {code} content={body.get('content')!r}")
    ad.close(); pre.stop()

    # 6 轮询中途 500：前两次历史读取失败，之后正常
    fake = FakeFay(port, lambda sent: [
        (0.0, [("add", ("fay", "扛住抖动。"))]),
    ], flaky=2)
    ad = Adapter(port)
    code, body = ad.post("/api/chat", {"content": "抖动一下", "username": "elder_1"})
    check(code == 200 and body.get("content") == "扛住抖动。",
          "6 轮询抛 500 不整体放弃", f"HTTP {code} content={body.get('content')!r}")
    ad.close(); fake.stop()

    # 5 只有 user 行：非 fay 行不能当回答
    fake = FakeFay(port, lambda sent: [(0.0, [("add", ("fay", "占位"))])])
    fake.script = lambda sent: []          # 只落 user 行，不出 fay 行
    ad = Adapter(port, adapter_max_wait_seconds=1, adapter_poll_interval=0.05)
    code, body = ad.post("/api/chat", {"content": "你好", "username": "elder_2"})
    check(code == 502 and "超时" in str(body.get("error")),
          "5/7 没有 type='fay' 新行时按上限返回 502", f"HTTP {code} error={body.get('error')!r}")
    ad.close(); fake.stop()

    # 7 超时文案带上限与用户名（后端 fay_gateway.py:57-63 靠 >=400 判失败）
    fake = FakeFay(port, lambda sent: [])
    ad = Adapter(port, adapter_max_wait_seconds=1)
    code, body = ad.post("/api/chat", {"content": "别说话", "username": "elder_42"})
    err = str(body.get("error") or "")
    check(code == 502 and "1s" in err and "elder_42" in err,
          "7 超时 502 的文案可定位（时长 + 用户名）", err)
    ad.close(); fake.stop()

    # 8 Fay 完全不可达：指向一个没人听的端口
    ad = Adapter(19777, adapter_max_wait_seconds=1)
    code, body = ad.post("/api/chat", {"content": "喂", "username": "elder_1"})
    err = str(body.get("error") or "")
    check(code == 502 and "历史读取失败" in err,
          "8 Fay 不可达时报「历史读取失败」而不是超时", f"HTTP {code} {err}")
    ad.close()

    # 11/12 是一对**只差哨兵**的对照：同一套两阶段脚本、同一个 settle=8。
    # 11 有哨兵 → 3.4s 上下收工；12 没哨兵（= 镜像没按 patches/fay/0008 重 build）
    # → 只能等静默，11.2s 上下收工。两条都必须是**洗干净的正文**，这正说明
    # 「正确性」靠的是 settle 2→8 + _clean，哨兵买的是**不用白等那 8 秒**。
    code, body, took = run_two_phase(port, two_phase(), 8, 25)
    content = str(body.get("content") or "")
    check(code == 200 and content == BODY,
          "11 两阶段回答：只留正文，占位句/prestart/think 全洗掉", f"HTTP {code} {content!r}")
    check("<think" not in content and "<prestart" not in content
          and "我来帮你查" not in content and "dh-end" not in content,
          "11b 清洗后不含任何协议噪音", repr(content))
    check(GAP <= took < GAP + 2.0,
          "11c 看到哨兵立刻收工，不等静默", f"{took:.1f}s（静默退路要 {GAP + 8:.0f}s）")

    code, body, took = run_two_phase(port, two_phase(with_sentinel=False), 8, 25)
    content = str(body.get("content") or "")
    check(code == 200 and content == BODY and took > GAP + 2.0,
          "12 没哨兵时退回静默判定，内容仍然是洗干净的正文",
          f"HTTP {code} {content!r} 用了 {took:.1f}s")

    # 13 只吐占位句 + think、没有正文：不能返回空 200 糊弄后端
    #    （上游 nlp_cognitive_stream.py:2277 的占位句 + 共 0 步的 think 块，
    #      就是本轮真实故障现场里 H5 看到的那一行）
    noise_only = [(0.0, [("add", ("fay", ACK))]),
                  (0.2, [("grow", (-1, ACK + "<think>\n执行耗时: 0.3s，共 0 步\n</think>\n"))]),
                  (0.4, [("grow", (-1, ACK + "<think>\n执行耗时: 0.3s，共 0 步\n</think>\n" + SENTINEL))])]
    code, body, took = run_two_phase(port, noise_only, 8, 25)
    err = str(body.get("error") or "")
    check(code == 502 and "没有给出正文" in err,
          "13 清洗后为空 → 502 而不是空回答", f"HTTP {code} {err}")

    # 15 正文**迟到**过整个静默窗口：库里先只有占位句，正文在 settle 的若干倍之后才写进来。
    #    这是 2026-09-22 真链路里红过的那一类（远端 26B 有一轮 17.8s 才出全，另一轮
    #    12.4s 一个字没出）：静默退路若在「手里还没有正文」时就收工，交出去的是空，
    #    于是把一次「慢但会来」的回答报成「本轮没有给出正文」。等下去不亏，MAX_WAIT 兜底。
    LATE = 7.0
    late_body = [(0.0, [("add", ("fay", ACK))]),
                 (LATE, [("grow", (-1, NOISE + BODY))])]
    code, body, took = run_two_phase(port, late_body, 1.5, 25)
    content = str(body.get("content") or "")
    check(code == 200 and content == BODY and took > LATE,
          "15 正文迟到过静默窗口时继续等，不提前交空正文",
          f"HTTP {code} {content!r} 用了 {took:.1f}s")

    # 14 反向对照：拔掉哨兵判定，11c 这条必须红（否则它没有真的挂在哨兵上）
    negative_control(port)

    bad = sum(1 for ok, _, _ in RESULTS if not ok)
    print(f"\n[adapter-test] {len(RESULTS) - bad}/{len(RESULTS)} 通过"
          + ("" if not bad else f"，{bad} 项失败"), flush=True)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}  {detail}", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
