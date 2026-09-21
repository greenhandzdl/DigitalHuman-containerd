#!/usr/bin/env python3
"""外壳那条 WebSocket 同源转发（/funasr-ws）的契约测试。

被测对象是镜像里那份 /app/carecho_web.py 的 `WSRelayMixin` + `open_tunnel`，
假上游（一个手写 TCP + 手搓 101 的线程）起在**同一容器**里，所以这组不碰 funasr、
不碰 backend、不出网 —— 十条判据在任何机器上都是秒级。

为什么值得单开一组：这条链是整栈里唯一「做错不报错、只是安静地坏」的地方。三处陷阱
（外壳 README 里也记着）分别对应这里的三条判据：

  * 处理线程一返回，连接就被当成 HTTP 继续读下一个请求 → 判据 6/7 会看到半截数据
  * 从 socket 而不是 rfile 读客户端 → 握手那一包里已经被 BufferedReader 吞掉的
    首帧字节凭空消失（用户症状：按了麦克风，第一个字永远缺）→ 判据 7 + 负面自检 B
  * 顺手解析/重写帧 → 掩码和扩展协商就会不对称 → 判据 6/8 会看到字节对不上

判据：
  1 表内路径但没带 Upgrade → 400，且假上游一个连接都没收到
  2 表外路径带着 Upgrade → 404，同样不拨号（转发目标只来自 CARECHO_WS_RELAY 这张表）
  3 带 query 的 /funasr-ws?sid=3 仍命中表，且上游看到的请求行是它自己的 /
  4 101 原样透传：Sec-WebSocket-Accept 能由我们发出的 Key 算出来
  5 头改写只发生在 Host/Connection/Upgrade 这三处，其余（含自定义头、Key）原样，
    且这三个头**每个只出现一次**（客户端那条 Upgrade 必须剔掉再补，见 open_tunnel 里的注释）
  6 双向字节数与顺序一致：一帧 70KB（> 单块 64KB）+ 五帧小音频突发，全部逐字节相同
  7 握手与首帧写在同一个 TCP 段里时，首帧不丢
  8 控制帧不解析：PING 原样到上游、PONG 原样回客户端
  9 上游发完最后一帧再关：客户端先拿到那一帧、然后才看到 EOF（收尾不吃 final）
 10 空闲到 WS_RELAY_IDLE 就双关，且外壳还能继续服务下一条连接（不泄漏半开隧道）
 11 负面自检 A：转发表的目标主机换成一个不存在的地址 → 判据 4 必须变红
 12 负面自检 B：客户端方向改成绕过 rfile 直接 recv → 判据 7 必须变红
 13 负面自检 C：让 Upgrade 被透传两遍（回到本轮真实故障）→ 握手必须上不去

只用标准库（和外壳一样）。帧的构造在 probes/wsutil.py，那份是客户端方向的掩码位
——浏览器只会发掩码帧，所以这里也必须发掩码帧，否则「纯透传」这条性质根本没被检验。
假上游的严度照真上游钉（wsutil.server_handshake 会拒绝重复的握手头）：它比真上游宽松一寸，
转发侧的 bug 就会在生产里才露头 —— 判据 5 与自检 C 是为此而加的。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wsutil as W  # noqa: E402  - 同目录共享帧构造器

CARECHO = os.environ.get("CARECHO_PATH") or "/app/carecho_web.py"
STATIC = os.environ.get("CARECHO_STATIC_UNDER_TEST", "/app/dist")

SHELL_PORT = 19901
UP_PORT = 19902
RELAY_SPEC = f"/funasr-ws=127.0.0.1:{UP_PORT}/"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


# --------------------------------------------------------------------------- 假上游
class Conn:
    """一条已握手的上游侧连接，由测试主线程驱动（不靠回调，读起来才是顺序的）。"""

    def __init__(self, sock: socket.socket, path: str, headers: dict, preset: bytes) -> None:
        self.sock = sock
        self.path = path
        self.headers = headers
        self.reader = W.Reader(sock, preset)
        self.closed_by_peer = False

    def next_frame(self, timeout: float = 5.0):
        """→ (opcode, payload)；None 表示对端关闭（EOF）。"""
        self.sock.settimeout(timeout)
        try:
            return self.reader.frame()
        except ConnectionError:
            self.closed_by_peer = True
            return None
        except (socket.timeout, OSError):
            return None

    def send(self, opcode: int, payload: bytes) -> None:
        # 服务端方向不掩码；这正是外壳不需要碰的东西
        self.sock.sendall(W.encode_frame(payload, opcode=opcode, mask=False))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class FakeUpstream:
    def __init__(self, port: int = UP_PORT) -> None:
        self.conns: list[Conn] = []
        # 被拒的握手原文留在这儿：判据 4 红的时候，detail 要能说出"上游为什么拒"，
        # 而不是只报"没连上"。本轮真实故障（转发多带一条 Upgrade → 426）就靠它点名。
        self.rejections: list[str] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            try:
                path, headers, rest = W.server_handshake(client)
            except W.HandshakeError as exc:
                self.rejections.append(str(exc))
                continue
            except (ConnectionError, OSError):
                client.close()
                continue
            conn = Conn(client, path, headers, rest)
            with self.lock:
                self.conns.append(conn)

    def accept(self, timeout: float = 6.0) -> Conn | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if self.conns:
                    return self.conns.pop(0)
            time.sleep(0.05)
        return None

    def conn_count(self) -> int:
        with self.lock:
            return len(self.conns)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- 被测外壳
class Shell:
    """起一份 carecho_web.py 子进程，转发目标指向上面那个假上游。"""

    def __init__(self, port: int = SHELL_PORT, spec: str = RELAY_SPEC,
                 path: str = CARECHO, idle: float = 20) -> None:
        env = {**os.environ, "CARECHO_PORT": str(port), "CARECHO_STATIC": STATIC,
               "CARECHO_WS_RELAY": spec, "CARECHO_WS_RELAY_IDLE": str(idle),
               "CARECHO_BACKEND": "http://127.0.0.1:1", "CARECHO_LOG": "WARNING"}
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen([sys.executable, path], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                self.get("/api/health")
                break
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"外壳没起来（退出码 {self.proc.returncode}）")
                time.sleep(0.2)
        else:
            raise RuntimeError("外壳 20s 内没应答 /api/health")

    def get(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def ws(self, path: str = "/funasr-ws", extra: tuple[str, ...] = (), key: str | None = None):
        """真客户端形态：握完手返回 (响应头, Reader, 原始 socket, 我们发出的 Key)。"""
        sock = W.connect("127.0.0.1", self.port, timeout=15)
        key = key or W.make_key()
        try:
            _, headers, reader = W.client_handshake(sock, path, f"127.0.0.1:{self.port}", extra, key)
        except W.HandshakeError as exc:
            sock.close()
            raise
        return headers, reader, sock, key

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def send_frames(sock: socket.socket, payloads: list[bytes]) -> None:
    for p in payloads:
        sock.sendall(W.encode_frame(p, opcode=W.OP_BINARY, mask=True))


# --------------------------------------------------------------------------- 判据主体
def run_checks(shell: Shell, upstream: FakeUpstream) -> dict[str, bool]:
    """跑 1-10，返回名字→结果（负面自检 B 要挑出判据 7 单独重跑）。"""
    out: dict[str, bool] = {}

    # 1 表内但没带 Upgrade：挡在拨号之前，否则任何人都能用它探内网端口
    code, body, _ = shell.get("/funasr-ws")
    ok1 = code == 400 and b"WebSocket" in body and upstream.conn_count() == 0
    check(ok1, "1 /funasr-ws 不带 Upgrade → 400 且不拨号",
          f"HTTP {code} body={body[:60]!r} 上游连接数={upstream.conn_count()}")
    out["plain400"] = ok1

    # 2 表外路径带 Upgrade：一律 404，且一个字节都不往外发
    key2 = W.make_key()
    s2 = W.connect("127.0.0.1", shell.port)
    s2.sendall(W.handshake_request("/fay-ws", f"127.0.0.1:{shell.port}", key2))
    status_line = s2.recv(256).split(b"\r\n", 1)[0].decode("latin-1", "replace")
    s2.close()
    # 版本号不核对：外壳的 protocol_version 是 HTTP/1.0，而握手请求行是 1.1，
    # send_response 回的是自己的那个 —— 这条判据要的是 404 和「没拨号」。
    ok2 = " 404" in status_line and upstream.conn_count() == 0
    check(ok2, "2 表外路径带 Upgrade → 404 且不拨号（不是开放代理）",
          f"{status_line!r} 上游连接数={upstream.conn_count()}")
    out["tableonly"] = ok2

    # 3 query 只用于查表前的剥离，上游看到的是它自己那条 path
    _, r3, s3, _ = shell.ws("/funasr-ws?sid=3&x=%2Fetc%2Fpasswd")
    conn3 = upstream.accept()
    ok3 = conn3 is not None and conn3.path == "/"
    check(ok3, "3 /funasr-ws?sid=3 命中表，且客户端给的 path 不进上游请求行",
          f"上游收到 {conn3.path if conn3 else '（没连上）'!r}")
    out["query"] = ok3
    s3.close(); conn3 and conn3.close()

    # 4/5 一次完整握手，顺带核头
    key4 = W.make_key()
    try:
        hdr4, r4, s4, _ = shell.ws(extra=("X-Relay-Probe: keepme",), key=key4)
        why401 = ""
    except (W.HandshakeError, ConnectionError) as exc:
        # 上游在握手阶段就拒了 —— 这正是生产里那条链的真实坏法（转发多带一条 Upgrade，
        # 真上游回 426）。判据要红并说出原因，不能让整个测试组以 traceback 收场。
        hdr4, r4, s4, why401 = {}, None, None, f"{exc.__class__.__name__}: {exc}"
    conn4 = upstream.accept()
    ok4 = hdr4.get("upgrade", "").lower() == "websocket" and conn4 is not None
    check(ok4, "4 转发后拿到 101，Sec-WebSocket-Accept 与 Key 算得一致",
          (f"上游连接={'有' if conn4 else '无'} upgrade={hdr4.get('upgrade')!r}"
           + (f" 上游拒绝={upstream.rejections[-1]}" if why401 and upstream.rejections else "")
           + (f" 客户端侧={why401}" if why401 else "")))
    out["handshake"] = ok4
    h = conn4.headers if conn4 else {}
    # Host 必须被改写成上游地址：留着外壳自己那个 Host，有些上游会拒绝握手
    # duplicates 那一条盯的是"转发时把头写了两遍"：dict 读不出重复，而严一点的上游读得出。
    dupes = getattr(h, "duplicates", [])
    ok5 = (h.get("host") == f"127.0.0.1:{UP_PORT}"
           and h.get("connection", "").lower() == "upgrade"
           and h.get("upgrade", "").lower() == "websocket"
           and h.get("sec-websocket-key") == key4
           and h.get("x-relay-probe") == "keepme"
           and not dupes
           and f"127.0.0.1:{SHELL_PORT}" not in json.dumps(dict(h)))
    check(ok5, "5 只改写 Host/Connection/Upgrade 且每条只发一次，Key 与自定义头原样带到上游",
          f"host={h.get('host')!r} Key 相同={h.get('sec-websocket-key') == key4} "
          f"x-relay-probe={h.get('x-relay-probe')!r} 重复头={dupes}")
    out["headers"] = ok5
    if not conn4:
        return out                       # 后面每条都要用这条连接，没有就别硬跑

    # 6 双向一致：一帧 70KB（超过 64KB 单块，逼出分次 recv）+ 五帧小突发
    payloads = [bytes((i * 7) % 256 for i in range(70_000))]
    send_frames(s4, payloads)
    got = conn4.next_frame(timeout=8)
    big_ok = got is not None and got[0] == W.OP_BINARY and got[1] == payloads[0]
    burst = [b"\x00" * 12 + bytes([n]) * n for n in range(1, 6)]
    send_frames(s4, burst)
    received = []
    for _ in burst:
        f = conn4.next_frame(timeout=8)
        if f is None:
            break
        received.append(f[1])
    ok6 = big_ok and received == burst
    check(ok6, "6 上行字节数与顺序一致（含 70KB 单帧，逐字节相同）",
          f"70KB 完整={big_ok} 突发 {len(received)}/{len(burst)} 帧顺序一致={received == burst}")
    out["bytes"] = ok6

    # 下行：上游发三帧，客户端必须按同一顺序收到（Reader 里可能已有多余字节）
    for text in ("第一段", "第二段", "最后一段"):
        conn4.send(W.OP_TEXT, json.dumps({"text": text, "is_final": text == "最后一段"},
                                         ensure_ascii=False).encode())
    down = []
    try:
        for _ in range(3):
            op, payload = r4.frame()
            down.append(json.loads(payload)["text"])
    except (ConnectionError, socket.timeout) as exc:
        down.append(f"<{exc.__class__.__name__}>")
    check(down == ["第一段", "第二段", "最后一段"], "6b 下行三帧顺序内容一致（含中文）", f"收到 {down}")
    out["bytes_down"] = down == ["第一段", "第二段", "最后一段"]
    s4.close(); conn4.close()

    # 7 握手与首帧同一个 write —— BufferedReader 会把首帧吞进用户态缓冲区的那种情形
    key7 = W.make_key()
    s7 = W.connect("127.0.0.1", shell.port)
    first = W.encode_frame(b"HEAD-FIRST-FRAME", opcode=W.OP_BINARY, mask=True)
    s7.sendall(W.handshake_request("/funasr-ws", f"127.0.0.1:{shell.port}", key7) + first)
    _, _, r7 = W.read_handshake_response(s7, key7)
    conn7 = upstream.accept()
    early = conn7.next_frame(timeout=6) if conn7 else None
    # 两种"没丢"的形态都算过：随握手响应一起到（rest 里），或稍后被泵到 —— 只要字节完整
    ok7 = early is not None and early[1] == b"HEAD-FIRST-FRAME"
    check(ok7, "7 握手与首帧同段发出时首帧不丢（rfile 那条陷阱）",
          f"上游首帧={early!r}")
    out["firstframe"] = ok7
    s7.close(); conn7.close()

    # 8 控制帧原样透传（不解析 = 不吞 ping/pong）
    ping_payload = "探".encode()
    _, r8, s8, _ = shell.ws()
    conn8 = upstream.accept()
    s8.sendall(W.encode_frame(ping_payload, opcode=W.OP_PING, mask=True))
    cf = conn8.next_frame(timeout=6)
    ping_ok = cf is not None and cf[0] == W.OP_PING and cf[1] == ping_payload
    conn8.send(W.OP_PONG, ping_payload)
    try:
        op8, pay8 = r8.frame()
        pong_ok = op8 == W.OP_PONG and pay8 == ping_payload
    except (ConnectionError, socket.timeout):
        pong_ok = False
    check(ping_ok and pong_ok, "8 PING/PONG 控制帧原样双向透传（不重开一帧）",
          f"到上游={ping_ok} 回客户端={pong_ok}")
    out["control"] = ping_ok and pong_ok
    s8.close(); conn8.close()

    # 9 上游「发完最后一帧才关」：那一帧必须先到客户端（final 被吃掉 = 前端永远停在录音中）
    _, r9, s9, _ = shell.ws()
    conn9 = upstream.accept()
    s9.settimeout(8)
    final = json.dumps({"text": "收尾就这一帧", "is_final": True}, ensure_ascii=False).encode()
    conn9.send(W.OP_TEXT, final)
    conn9.sock.shutdown(socket.SHUT_WR)   # 上游说完就收工，但先不把连接整个掐掉
    try:
        op9, pay9 = r9.frame()
        ok9 = op9 == W.OP_TEXT and pay9 == final
    except (ConnectionError, socket.timeout) as exc:
        ok9 = False
        pay9 = f"<{exc.__class__.__name__}>".encode()
    check(ok9, "9 上游最后一帧不会因为收尾被吃掉", f"客户端收到 {pay9[:60]!r}")
    out["drain"] = ok9
    s9.close(); conn9.close()

    # 10 空闲到点双关，且外壳不受影响（不泄漏半开隧道）
    idle = Shell(port=SHELL_PORT + 6, spec=RELAY_SPEC, idle=3)
    try:
        _, r10, s10, _ = idle.ws()
        conn10 = upstream.accept()
        t0 = time.time()
        s10.settimeout(12)
        eof_client = False
        try:
            while True:
                if not s10.recv(4096):
                    eof_client = True
                    break
        except (ConnectionError, socket.timeout, OSError):
            eof_client = False
        secs = time.time() - t0
        eof_up = conn10.next_frame(timeout=6) is None
        still_alive = idle.get("/api/health")[0] == 200
        ok10 = eof_client and eof_up and 2.0 < secs < 9.0 and still_alive
        check(ok10, "10 空闲 3s 后两侧都收到 EOF，外壳照常服务下一条连接",
              f"客户端 EOF={eof_client} 上游 EOF={eof_up} 等了 {secs:.1f}s 健康口={still_alive}")
        out["idle"] = ok10
        s10.close(); conn10.close()
    finally:
        idle.close()
    return out


# --------------------------------------------------------------------------- 负面自检
def mutate(anchor: str, replacement: str, dst: str) -> str | None:
    with open(CARECHO, encoding="utf-8") as fh:
        src = fh.read()
    broken = src.replace(anchor, replacement, 1)
    if broken == src:
        return None
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(broken)
    return dst


def negative_a(upstream: FakeUpstream) -> bool:
    """转发表里的目标主机改成一个查无此名的地址：握手那条判据必须撑不住。

    假上游照常监听 —— 否则"失败"可能只是因为没人接，而不是因为目标被改坏了。
    改的必须是**主机名**：往串尾加 "x" 会落到路径上（"127.0.0.1:19902/" → ".../x"），
    而假上游不看路径，那种"改坏"是惰性的 —— 实测拿不到红。
    """
    path = mutate("table[path.strip()] = upstream.strip()",
                  'table[path.strip()] = "no-such-host." + upstream.strip()',
                  "/tmp/carecho_relay_broken_a.py")
    if path is None:
        print("FAIL  11 负面自检 A 没能改坏任何东西（替换锚点失效，这份自检已不可信）")
        return False
    shell = Shell(port=SHELL_PORT + 2, spec=RELAY_SPEC, path=path)
    try:
        try:
            shell.ws()
            mutated = False
        except (W.HandshakeError, ConnectionError):
            mutated = True
    finally:
        shell.close()
    print(f"{'PASS' if mutated else 'FAIL'}  11 负面自检 A：转发表目标换成不存在的地址后握手确实上不去"
          f"  —— 变红={mutated}", flush=True)
    return mutated


def negative_b(upstream: FakeUpstream) -> bool:
    """把 _client_read 从 rfile 改成直接 recv：判据 7（首帧不丢）必须变红。

    这条自检是这组测试里最值钱的一条 —— 它证明判据 7 真的在盯那个缓冲区，
    而不是碰巧通过。
    """
    path = mutate("        return h.rfile.read1(WS_RELAY_CHUNK)",
                  "        return h.connection.recv(WS_RELAY_CHUNK)",
                  "/tmp/carecho_relay_broken_b.py")
    if path is None:
        print("FAIL  12 负面自检 B 没能改坏任何东西（替换锚点失效，这份自检已不可信）")
        return False
    shell = Shell(port=SHELL_PORT + 4, spec=RELAY_SPEC, path=path)
    try:
        key = W.make_key()
        s = W.connect("127.0.0.1", shell.port)
        s.sendall(W.handshake_request("/funasr-ws", f"127.0.0.1:{shell.port}", key)
                  + W.encode_frame(b"HEAD-FIRST-FRAME", opcode=W.OP_BINARY, mask=True))
        W.read_handshake_response(s, key)
        conn = upstream.accept()
        got = conn.next_frame(timeout=4) if conn else None
        mutated = got is None or got[1] != b"HEAD-FIRST-FRAME"
        s.close()
        if conn:
            conn.close()
    except (OSError, W.HandshakeError) as exc:
        # 整个握手都坏了也算变红：说明这条判据不是靠运气过的
        mutated = True
        print(f"     （改坏后连带握手也失败：{exc.__class__.__name__}）")
    finally:
        shell.close()
    print(f"{'PASS' if mutated else 'FAIL'}  12 负面自检 B：绕过 rfile 直接 recv 后首帧确实丢了"
          f"  —— 变红={mutated}", flush=True)
    return mutated


def negative_c(upstream: FakeUpstream) -> bool:
    """把剔除名单里的 "upgrade" 去掉（= 客户端那条 Upgrade 被透传、外壳又补一条）：判据必须变红。

    这条不是编出来的场景 —— 2026-09-21 生产里就是它：那时假上游用 dict 收头，重复被折叠掉，
    13 条判据全绿，而真上游 websockets 16.1 回 426，手机上一句话都识别不了。
    改完假上游会拒，所以这里挑的是「握手不再成功」这件事本身。
    """
    path = mutate('"host", "connection", "upgrade", "keep-alive", "proxy-connection"',
                  '"host", "connection", "keep-alive", "proxy-connection"',
                  "/tmp/carecho_relay_broken_c.py")
    if path is None:
        print("FAIL  13 负面自检 C 没能改坏任何东西（替换锚点失效，这份自检已不可信）")
        return False
    shell = Shell(port=SHELL_PORT + 8, spec=RELAY_SPEC, path=path)
    try:
        try:
            _, _, _, _ = shell.ws()
            mutated = False
        except (W.HandshakeError, ConnectionError):
            mutated = True
    finally:
        shell.close()
    print(f"{'PASS' if mutated else 'FAIL'}  13 负面自检 C：多带一条 Upgrade 后上游确实拒了握手"
          f"  —— 变红={mutated}"
          + (f"  —— 上游拒绝={upstream.rejections[-1]}" if mutated and upstream.rejections else ""),
          flush=True)
    return mutated


def main() -> int:
    if not os.path.isdir(STATIC):
        print(f"[ws-relay-test] 找不到静态目录 {STATIC}（这组要跑在 dh-frontend:local 里）", file=sys.stderr)
        return 2
    # 一个假上游贯穿全程：负面自检要的是「除了那一处改动，别的东西都一样」。
    upstream = FakeUpstream()
    shell = Shell()
    try:
        results = run_checks(shell, upstream)
        red_a = negative_a(upstream)
        red_b = negative_b(upstream)
        red_c = negative_c(upstream)
    finally:
        shell.close()
        upstream.stop()

    good = sum(1 for ok, _, _ in RESULTS if ok)
    negatives = (red_a, red_b, red_c)
    total = len(RESULTS) + len(negatives)
    failed = total - good - sum(1 for r in negatives if r)
    print(f"\n[ws-relay-test] {total - failed}/{total} 通过"
          + ("" if failed == 0 else f"，{failed} 项失败")
          + ("" if all(negatives) else "（负面自检没能把对应判据弄红，这套判据是摆设）"))
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}  {detail}")
    untested = [k for k, v in results.items() if not v]
    if untested:
        print(f"  涉及判据：{' '.join(untested)}")
    return 0 if failed == 0 and all(negatives) else 1


if __name__ == "__main__":
    sys.exit(main())
