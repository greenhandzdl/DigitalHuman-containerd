"""CareEcho H5 的容器侧外壳：静态托管 + 把它真正会调的那个接口接到本栈后端 + 同源 WS 转发。

前端仓库（gitee `xie-zha-zha/carecho_final`）是纯 Vite 工程：`npm run dev` 靠 vite 的
proxy 把 `/api` 转到它假想的 `127.0.0.1:5001`、把 `/funasr-ws` 转到 `127.0.0.1:10095`，
而 `npm run build` 出来的 `dist-h5` 没有那层 proxy —— 所以产物要能跑，得有个同源的服务端
同时干三件事：发静态文件、把 `/api` 顶起来、把那条 WebSocket 转发出去。这份就是这三者，
只用标准库（先例是 `adapter/server.py`：为两个接口拉一套 web 框架不值）。

HTTP 侧只实现它真会发的那一个口。全仓 grep 过 `src/` 里对 `api/` 的调用点，命中的只有
`App.vue:100` 的 `chatAPI.sendMessage`（`POST /api/chat/send`）；`chatAPI` 其余方法和
`appointmentAPI` 整组都只有定义、没有调用方（后端那边也确实没有挂号域）。剩下的外部
依赖魔珐数字人走它自己的云 SDK + 密钥，这里不假装支持，宁可让前端自己的错误分支报出来。
FunASR 那条 ws 是本轮补上的：`CARECHO_WS_RELAY` 那张常量表见 `_relay_table` 的 docstring。
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import socket
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = os.environ.get("CARECHO_BACKEND", "http://backend:8000").rstrip("/")
STATIC = os.environ.get("CARECHO_STATIC", "/app/dist")
# 浏览器侧 axios 的 timeout 是前端写死的 30s（request.js:6）。这里给到 90s 是让它成为
# 唯一的上限，而不是我们自己再加一道更短的先掐掉；超过 30s 的回答在浏览器那端会先超时，
# 那是前端自己的常量，不改它。
UPSTREAM_TIMEOUT = float(os.environ.get("CARECHO_UPSTREAM_TIMEOUT", "90"))


def _relay_table() -> dict[str, str]:
    """`CARECHO_WS_RELAY` 形如 `/funasr-ws=funasr:10095/`，逗号可分隔多条。

    转发目标**只能来自这张常量表**：客户端给的 path 只用来查表、不参与拨号，
    表里也没有重定向或 scheme 解析的余地 —— 所以这不是一个开放代理（SSRF 那道门是关的）。
    表外路径即使带着 Upgrade 头也不转，直接 404。
    """
    table: dict[str, str] = {}
    for item in os.environ.get("CARECHO_WS_RELAY", "/funasr-ws=funasr:10095/").split(","):
        path, sep, upstream = item.partition("=")
        if sep and path.strip().startswith("/") and upstream.strip():
            table[path.strip()] = upstream.strip()
    return table


WS_RELAY = _relay_table()
# 转发期两侧的空闲上限。ASR 会话本身有 FUNASR_MAX_SECONDS(60) 兜底，这个值只管
# 「浏览器开着页面不动」那一段：空闲到点就断，用户下次按麦克风会重连（funasr.js 每次
# start 都 new WebSocket），所以这里不需要保活。
WS_RELAY_IDLE = float(os.environ.get("CARECHO_WS_RELAY_IDLE", "180"))
WS_RELAY_CONNECT = float(os.environ.get("CARECHO_WS_RELAY_CONNECT", "10"))
# 握手之后纯粹搬字节，单帧最大 64KB 的 PCM（4096 样点 ×2 字节 = 8KB，用不完）
WS_RELAY_CHUNK = 65536


def _ws_upgrade_wanted(headers) -> bool:
    """只认「完整的 WS 握手」：Connection 含 upgrade + Upgrade: websocket + Key + Version 13。

    四个条件都必要，否则会咬人：漏了 Version 13 的伪握手上游根本不会回 101，
    而我们已经把这条连接当隧道交出去了，用户看到的是无限期的"正在录音"。
    """
    conn = [tok.strip().lower() for tok in (headers.get("Connection") or "").split(",")]
    return ("upgrade" in conn
            and (headers.get("Upgrade") or "").strip().lower() == "websocket"
            and bool(headers.get("Sec-WebSocket-Key"))
            and (headers.get("Sec-WebSocket-Version") or "").strip() == "13")


def _split_upstream(spec: str) -> tuple[str, int, str]:
    """`funasr:10095/` → ("funasr", 10095, "/")。只接受 host[:port][/path]，端口缺省 80。"""
    host, _, path = spec.partition("/")
    hostname, sep, port = host.partition(":")
    return hostname, (int(port) if sep and port else 80), ("/" + path if path else "/")


def _plain(h: BaseHTTPRequestHandler, code: int, msg: str) -> None:
    """给 WS 客户端看的错误：浏览器只会把它报成「握手失败 status=N」，正文进不了 onmessage。

    所以这一层宁可用纯文本 + 明确状态码，也不套 `{code,data,msg}` 那个信封 —— 那个是
    给 axios 拦截器用的，握手阶段还没到 JS 的响应处理，套了也没人读。
    """
    body = msg.encode()
    h.send_response(code)
    h.send_header("Content-Type", "text/plain; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def _client_read(h: BaseHTTPRequestHandler) -> bytes:
    """从浏览器侧取字节，**必须走 h.rfile**。

    不能 `h.connection.recv`：`StreamRequestHandler.rbufsize=-1` 那个 8KB BufferedReader
    在读握手请求时可能已经顺带把开头一两帧 PCM 吞进用户态缓冲区了，绕过缓冲区直接摸
    socket 就会丢那几字节 —— 表现正是「按了麦克风，第一个字永远缺」。
    反过来 select 只看得见 socket、看不见缓冲区，所以先非阻塞地把缓冲区里的货取干净，
    再决定这一轮等谁；顺序颠倒时会出现「明明有货却在等超时」。
    """
    h.connection.settimeout(0.0)
    try:
        return h.rfile.read1(WS_RELAY_CHUNK)
    except (BlockingIOError, socket.timeout):
        return b""
    finally:
        h.connection.settimeout(WS_RELAY_IDLE)


def open_tunnel(h: BaseHTTPRequestHandler, upstream: str) -> None:
    """把这条已握手前的连接改造成一条 WS 隧道，阻塞搬运直到一侧结束。

    三处做错就会静默坏掉的地方：

    1. **处理函数不许返回**。整段转发都在 do_GET 里跑完；一返回，
       BaseHTTPRequestHandler 就把这条连接当成 HTTP 继续读下一个请求，WS 帧会被当请求行解析。
       同理这里绝不调 `send_response` —— 101 是上游发的，我们只搬字节。
       `protocol_version` 保持默认 HTTP/1.0，`parse_request` 已经把 close_connection 置真，
       这条连接不会被当成 keep-alive 复用。
    2. **不解析帧**。掩码、分片、ping/pong、permessage-deflate 一律透传。自己组帧就得
       重做一遍扩展协商，那是新 bug 的源头；透传保证两端谈成什么样就是什么样。
    3. **收尾要先把上游的最后几帧送出去**。浏览器关连接时上游可能正在发最后一个 final，
       直接 close 会把那一帧吃掉 —— 而 final 吃掉等于前端永远停在"正在录音"。
    """
    host, port, path = _split_upstream(upstream)
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}"]
    # `upgrade` 必须在剔除名单里：客户端那条要由下面两行统一补回。少剔一次就发出两条
    # Upgrade，而真上游（websockets 16.1）见到重复的 Upgrade 直接回 426 —— 实测 2026-09-21：
    # 同一条握手单份头拿 101、双份头拿 426，外壳把 426 原样搬给浏览器，看起来像"上游拒了"。
    for name, value in h.headers.items():
        if name.lower() in ("host", "connection", "upgrade", "keep-alive", "proxy-connection"):
            continue
        lines.append(f"{name}: {value}")
    # 上面剔掉了 Connection/Upgrade，这里补回：上游就是靠这两个头判定要不要回 101 的。
    lines += ["Connection: Upgrade", "Upgrade: websocket"]
    try:
        remote = socket.create_connection((host, port), timeout=WS_RELAY_CONNECT)
    except OSError as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        log.error("WS %s → %s:%s 拨号失败：%s", h.path, host, port, reason)
        _plain(h, 502, f"语音服务不可达：{reason}")
        return

    client = h.connection
    client.settimeout(WS_RELAY_IDLE)
    remote.settimeout(WS_RELAY_IDLE)
    handed_off = False
    try:
        remote.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        head = bytearray()
        while b"\r\n\r\n" not in head:
            more = remote.recv(4096)
            if not more:
                raise ConnectionError("上游在握手完成前关闭")
            head += more
            if len(head) > 65536:
                raise ConnectionError("上游握手响应头过大")
        status, _, rest = bytes(head).partition(b"\r\n\r\n")
        h.wfile.write(status + b"\r\n\r\n")
        h.wfile.flush()
        handed_off = True
        log.info("WS %s ↔ %s:%s%s：%s", h.path, host, port, path,
                 status.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
        if rest:  # 多读到的那部分已经是帧了，先泵给浏览器
            h.wfile.write(rest)
            h.wfile.flush()
        _pump(h, remote)
    except (OSError, ConnectionError) as exc:
        log.info("WS %s 收尾：%s", h.path, exc.__class__.__name__)
    finally:
        if handed_off:
            _drain_upstream(h, remote)
        else:
            try:
                remote.close()
            except OSError:
                pass
        h.close_connection = True


def _pump(h: BaseHTTPRequestHandler, remote: socket.socket) -> None:
    while True:
        data = _client_read(h)
        if data:
            remote.sendall(data)
            continue
        try:
            readable, _, _ = select.select([h.connection, remote], [], [], WS_RELAY_IDLE)
        except (OSError, ValueError):
            return
        if not readable:
            log.info("WS %s 空闲 %ds，关闭", h.path, int(WS_RELAY_IDLE))
            return
        for sock in readable:
            if sock is h.connection:
                data = h.rfile.read1(WS_RELAY_CHUNK)
                if not data:  # 浏览器先关了：让上游自己收完并正常结束这一路
                    return
                try:
                    remote.sendall(data)
                except OSError:
                    return
            else:
                try:
                    data = remote.recv(WS_RELAY_CHUNK)
                except OSError:
                    return
                if not data:
                    return
                try:
                    h.wfile.write(data)
                    h.wfile.flush()
                except OSError:
                    return


def _drain_upstream(h: BaseHTTPRequestHandler, remote: socket.socket) -> None:
    """对上游 shutdown(SHUT_WR) + 最多 2s 收尾，把它还想发的最后几帧送完再双关。

    浏览器那侧看到的是 `wasClean=false` 的关闭 —— funasr.js 的 onclose 只把按钮复位、
    不读事件字段，所以这里不需要（也没法）替它伪装成一个干净关闭。
    """
    try:
        remote.shutdown(socket.SHUT_WR)
        remote.settimeout(2.0)
        while True:
            data = remote.recv(4096)
            if not data:
                break
            h.wfile.write(data)
            h.wfile.flush()
    except OSError:
        pass
    finally:
        try:
            remote.close()
        except OSError:
            pass


class WSRelayMixin:
    """do_GET 第一步：判断这条请求是不是（或本该是）WS 转发，是就整个处理掉。

    单独成 mixin 而不是写在 Handler 里，是为了让 probes/ws_relay_test.py 用一个不带静态目录、
    不带后端的最小 handler 复用**同一份**代码 —— 要测的就是这段分派 + 隧道；
    复制一份去测等于没测。
    """

    def ws_dispatch(self) -> bool:
        path = self.path.split("?")[0]
        upgrade = _ws_upgrade_wanted(self.headers)
        upstream = WS_RELAY.get(path)
        if upstream is None and not upgrade:
            return False
        if upstream is None:
            # 表外的握手一律不碰网络：这是「不是开放代理」那条约束的另一半。
            _plain(self, 404, f"未开放 WebSocket 转发：{path}（可转发的路径见 CARECHO_WS_RELAY）")
            return True
        if not upgrade:
            _plain(self, 400, f"{path} 只接受 WebSocket 升级请求")
            return True
        open_tunnel(self, upstream)
        return True


log = logging.getLogger("carecho_web")

# 设备 → {token, user_id, session_id}。前端没有任何登录调用，身份只能由这一侧给：
# 第一次见一个设备就后厨式地替它走一次 dev-login（后端 DEBUG=true 才有的口），
# 会话建好之后一直复用 —— 这就是「刷新页面还接得上同一段对话」的全部实现。
# 内存态是故意的：落盘会让重启后的容器替每个老 cookie 各建一份后端会话，那才是脏。
DEVICES: dict[str, dict] = {}
DEVICES_LOCK = threading.Lock()


class Upstream(Exception):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


def _backend_call(method: str, path: str, payload: dict | None, token: str | None, timeout: float) -> dict:
    data = json.dumps(payload).encode() if payload is not None else b""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BACKEND}/api/v1{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise Upstream(f"后端 {method} {path} → HTTP {exc.code} {detail}", status=exc.code) from exc
    except OSError as exc:
        # 这行文案会直接进气泡（前端拼的是「连接失败：<msg>」），所以别把 URLError 的
        # 包装层一起抖出来：URLError(ConnectionRefusedError(111, ...)) 对人没用。
        reason = getattr(exc, "reason", exc)
        raise Upstream(f"后端 {method} {path} 不可达：{reason}") from exc
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise Upstream(f"后端 {method} {path} 返回的不是 JSON：{body[:200]!r}") from exc


def _login(device_id: str) -> dict:
    login = _backend_call("POST", "/auth/dev-login", {"openid": f"h5_{device_id}"}, None, 30)
    token = login.get("access_token")
    if not token:
        raise Upstream(f"dev-login 没拿到 access_token：{str(login)[:200]}")
    sess = _backend_call("POST", "/chat/sessions", {}, token, 30)
    sid = sess.get("id")
    if not sid:
        raise Upstream(f"建会话失败：{str(sess)[:200]}")
    st = {"token": token, "user_id": sess.get("user_id") or login.get("user_id"), "session_id": int(sid)}
    log.info("设备 %s → user_id=%s session_id=%s", device_id[:8], st["user_id"], st["session_id"])
    return st


def _device_state(device_id: str) -> dict:
    """拿到（必要时就地建好）这个设备的 token 与会话。"""
    with DEVICES_LOCK:
        st = DEVICES.get(device_id)
        if st and st.get("session_id"):
            return dict(st)
    # 建会话是两次 POST，放锁外面：9b 冷启动那一下可能十几秒，攥着锁会把别的设备一起卡住。
    st = _login(device_id)
    with DEVICES_LOCK:
        DEVICES[device_id] = st
    return dict(st)


def _send_message(device_id: str, content: str) -> dict:
    st = _device_state(device_id)
    try:
        out = _backend_call(
            "POST", f"/chat/sessions/{st['session_id']}/messages", {"content": content},
            st["token"], UPSTREAM_TIMEOUT,
        )
    except Upstream as exc:
        # JWT 有效期 12 小时（JWT_EXPIRE_MINUTES=720），而这个进程可能比它活得久：
        # 老 cookie 上的 token 过期后每一发都是 401，不重登就永远修不好。重登一次 =
        # 换一个新会话，代价是前端那侧 sessionId 会跳一次，比一直 401 划算。
        if exc.status != 401:
            raise
        with DEVICES_LOCK:
            DEVICES.pop(device_id, None)
        log.info("设备 %s 的 token 已失效，重新登录并换一个会话", device_id[:8])
        st = _device_state(device_id)
        out = _backend_call(
            "POST", f"/chat/sessions/{st['session_id']}/messages", {"content": content},
            st["token"], UPSTREAM_TIMEOUT,
        )
    reply = (out.get("assistant_message") or {}).get("content")
    if not reply:
        # fay_forwarded=false 时后端仍会 201 回来，只是 assistant_message 是 null ——
        # 这一层不翻译成"空回复"，前端就该看见它没答。
        raise Upstream(f"数字人没有回复（fay_error={out.get('fay_error')!r}）")
    tier = out.get("tier_classification") or {}
    return {"session_id": st["session_id"], "reply": reply,
            "risk_tier": tier.get("tier"), "fay_forwarded": out.get("fay_forwarded")}


class Handler(WSRelayMixin, BaseHTTPRequestHandler):
    server_version = "carecho-web/1"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _api(self, code: int, data=None, msg: str | None = None, extra: dict | None = None) -> None:
        # 前端 axios 的响应拦截器（request.js:16）只认 {code:200, data} 这个信封：
        # 不是 200 就 throw new Error(res.msg)，所以 msg 就是用户气泡上那行字。
        self._send(code, "application/json; charset=utf-8",
                   json.dumps({"code": code, "data": data, "msg": msg},
                              ensure_ascii=False).encode(), extra)

    def _device(self) -> str:
        raw = self.headers.get("Cookie") or ""
        got = re.search(r"carecho_device=([0-9a-f]{32})", raw)
        return got.group(1) if got else uuid.uuid4().hex

    def do_GET(self) -> None:
        # 麦克风那条 ws 必须排在静态之前：它的路径不在 dist 里，落到 _static() 就会被
        # SPA 兜底成 index.html(200)，浏览器于是报「握手状态码不是 101」，真因看不见。
        if self.ws_dispatch():
            return
        if self.path == "/api/health":
            self._api(200, {"ok": True, "backend": BACKEND, "devices": len(DEVICES),
                            "ws_relay": sorted(WS_RELAY)})
            return
        if self.path.startswith("/api/"):
            self._api(501, msg=f"本外壳只实现 /api/chat/send 与 /api/health，收到 {self.path}")
            return
        self._static()

    def do_HEAD(self) -> None:
        self._static()

    def do_POST(self) -> None:
        if not self.path.startswith("/api/"):
            self._api(405, msg="只有 /api/* 接受 POST")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._api(400, msg="请求体不是 JSON")
            return
        if self.path != "/api/chat/send":
            self._api(501, msg=f"未实现 {self.path}")
            return
        content = str(body.get("content") or "").strip()
        if not content:
            self._api(400, msg="content 为空")
            return
        cookie = ""
        try:
            device = self._device()
            data = _send_message(device, content)
            if f"carecho_device={device}" not in (self.headers.get("Cookie") or ""):
                cookie = f"carecho_device={device}; Path=/; SameSite=Lax; HttpOnly"
            self._api(200, data, extra={"Set-Cookie": cookie} if cookie else None)
        except Upstream as exc:
            log.error("chat/send 失败：%s", exc)
            self._api(502, msg=str(exc))

    def _static(self) -> None:
        rel = urllib.request.url2pathname(self.path.split("?")[0])
        root = os.path.realpath(STATIC)
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if not target.startswith(root + os.sep) and target != root:
            self.send_error(403, "out of root")
            return
        if not os.path.isfile(target):
            target = os.path.join(root, "index.html")
        if not os.path.isfile(target):
            self.send_error(503, f"{STATIC} 里没有 index.html（构建产物没挂上？）")
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json",
                 ".svg": "image/svg+xml", ".woff2": "font/woff2", ".png": "image/png",
                 ".ico": "image/x-icon"}.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        with open(target, "rb") as fh:
            body = fh.read()
        # 入口页不缓存：Vite 的 assets 带内容哈希，index.html 是唯一需要换新的那一层。
        extra = {"Cache-Control": "no-cache"} if ctype.startswith("text/html") else {}
        self._send(200, ctype, body, extra)


def main() -> int:
    logging.basicConfig(level=os.environ.get("CARECHO_LOG", "INFO").upper(),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stderr)
    if not os.path.isdir(STATIC):
        log.error("静态目录不存在：%s", STATIC)
        return 1
    host, port = os.environ.get("CARECHO_HOST", "0.0.0.0"), int(os.environ.get("CARECHO_PORT", "8080"))
    # 启动就把转发表打出来：表是常量、拨号目标是容器名，写错一个字符的表现是
    # 「点麦克风没反应」，日志里这一行能省掉半小时排查。
    log.info("CareEcho H5 外壳监听 %s:%s，静态 %s，后端 %s，WS 转发 %s",
             host, port, STATIC, BACKEND,
             ", ".join(f"{p}→{u}" for p, u in sorted(WS_RELAY.items())) or "（空表）")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
