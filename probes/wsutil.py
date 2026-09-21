#!/usr/bin/env python3
"""WebSocket 握手与帧的最小构造器 —— 只用标准库，够测试件用就停手。

为什么不 import websockets：这些脚本要跑在 dh-frontend:local 里（那镜像只有 stdlib 和
npm 产物），为两组测试往生产镜像里塞依赖是本末倒置。更要紧的是**客户端→服务端必须掩码**
（RFC 6455 §5.3），而外壳的转发是纯透传 —— 只有亲手发出真正的浏览器帧格式，才能证伪
「转发时顺手把掩码解了 / 把帧重组成新帧」这类实现（那样做在单帧测试里也看不出问题）。

帧格式只实现到这几点：单一分片（FIN=1）、掩码位按方向置、7/16/64 三档长度。
没有扩展（permessage-deflate 不协商）、没有分片重组 —— 外壳不解析帧，这两件事它不可能错，
而我们要测的正是"它不解析"这个性质本身。
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class HandshakeError(Exception):
    def __init__(self, msg: str, status_line: str = "") -> None:
        super().__init__(msg)
        self.status_line = status_line


def make_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def accept_for(key: str) -> str:
    """RFC 6455 §1.3 那个 sha1+GUID。客户端拿它核验握手，等于核验「101 真是上游给的」。"""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode()


def encode_frame(payload: bytes, opcode: int = OP_BINARY, mask: bool = False, fin: bool = True) -> bytes:
    """mask=True 是客户端方向；真实浏览器只会发掩码帧。"""
    head = bytearray([0x80 | opcode if fin else opcode])
    n = len(payload)
    mb = 0x80 if mask else 0x00
    if n < 126:
        head.append(mb | n)
    elif n < (1 << 16):
        head.append(mb | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mb | 127)
        head += struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


class Reader:
    """带缓冲的帧读取器。

    必须带缓冲：转发是中性的字节泵，一次 recv 很可能同时带回「半条握手响应」和
    「第一个完整帧」，甚至半个帧头。没有 preset 这个入口，测试就会把先读到的字节丢掉。
    """

    def __init__(self, sock: socket.socket, preset: bytes = b"") -> None:
        self.sock = sock
        self.buf = bytearray(preset)

    def _fill(self) -> None:
        more = self.sock.recv(65536)
        if not more:
            raise ConnectionError("对端关闭")
        self.buf += more

    def need(self, n: int) -> None:
        while len(self.buf) < n:
            self._fill()

    def frame(self) -> tuple[int, bytes]:
        """→ (opcode, payload)。payload 已按掩码位解回原样（服务端方向本来就不该掩）。"""
        self.need(2)
        b0, b1 = self.buf[0], self.buf[1]
        opcode, masked, n = b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
        off = 2
        if n == 126:
            self.need(off + 2)
            n = struct.unpack(">H", bytes(self.buf[off:off + 2]))[0]
            off += 2
        elif n == 127:
            self.need(off + 8)
            n = struct.unpack(">Q", bytes(self.buf[off:off + 8]))[0]
            off += 8
        if masked:
            self.need(off + 4)
            key = bytes(self.buf[off:off + 4])
            off += 4
        self.need(off + n)
        payload = bytes(self.buf[off:off + n])
        del self.buf[:off + n]
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def has_frame(self) -> bool:
        """缓冲区里是否已经躺着一个完整帧（用来断言「该来的都来了、没多来」）。"""
        if len(self.buf) < 2:
            return False
        n = self.buf[1] & 0x7F
        off = 2 + (2 if n == 126 else 8 if n == 127 else 0) + (4 if self.buf[1] & 0x80 else 0)
        try:
            if n == 126:
                n = struct.unpack(">H", bytes(self.buf[2:4]))[0]
            elif n == 127:
                n = struct.unpack(">Q", bytes(self.buf[2:10]))[0]
        except IndexError:
            return False
        return len(self.buf) >= off + n


def handshake_request(
    path: str, host: str, key: str | None = None, extra: tuple[str, ...] = ()
) -> bytes:
    """浏览器会发出去的那段握手请求，原样交给调用者（可以攒着和首帧一起 write）。"""
    key = key or make_key()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        *extra,
        "",
        "",
    ]
    return "\r\n".join(lines).encode("latin-1")


def read_handshake_response(sock: socket.socket, key: str, preset: bytes = b"") -> tuple[str, dict[str, str], Reader]:
    head = bytearray()
    while b"\r\n\r\n" not in head:
        more = sock.recv(4096)
        if not more:
            raise ConnectionError("握手期间对端关闭")
        head += more
        if len(head) > 65536:
            raise HandshakeError("握手响应头过大")
    block, _, rest = bytes(head).partition(b"\r\n\r\n")
    status, _, headers_block = block.partition(b"\r\n")
    status_line = status.decode("latin-1", "replace")
    headers: dict[str, str] = {}
    for line in headers_block.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name:
            headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    if not status_line.endswith(" 101 Switching Protocols"):
        raise HandshakeError(f"握手没成：{status_line}", status_line)
    if headers.get("sec-websocket-accept") != accept_for(key):
        raise HandshakeError(
            f"Sec-WebSocket-Accept 对不上（{headers.get('sec-websocket-accept')!r}），"
            "说明 101 不是上游原样给回来的", status_line)
    return status_line, headers, Reader(sock, preset + rest)


def client_handshake(
    sock: socket.socket, path: str, host: str, extra: tuple[str, ...] = (), key: str | None = None
) -> tuple[str, dict[str, str], Reader]:
    """发一条真实浏览器形态的握手，返回 (状态行, 响应头, 已带余量的 Reader)。

    key 可以指定：判据要拿它反查「上游收到的 Key 与我们发出的是同一个」，
    顺手也就覆盖了「转发过程中头被改写过没有」这一类实现。
    """
    key = key or make_key()
    sock.sendall(handshake_request(path, host, key, extra))
    return read_handshake_response(sock, key)


class RequestHeaders(dict):
    """握手请求头：dict 语义不变（同名取最后一条），额外把每个名字出现的次数记在 `.seen`。

    需要计数是因为 `dict` 会把重复头折叠掉 —— 而**重复本身就是 bug**：转发时把客户端那条
    `Upgrade` 透传过去、又自己补一条，上游收到两条 `Upgrade`。真上游 websockets 16.1 对这种
    请求回 426（实测），但旧的假上游看不见。现在它既能拒绝（见 server_handshake），
    也能在拒绝时把重复的名字报出来。
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[str, int] = {}

    def add(self, name: str, value: str) -> None:
        self[name] = value
        self.seen[name] = self.seen.get(name, 0) + 1

    @property
    def duplicates(self) -> list[str]:
        return sorted(n for n, c in self.seen.items() if c > 1)


def server_handshake(sock: socket.socket) -> tuple[str, RequestHeaders, bytes]:
    """假上游用：读掉一条握手请求并直接回 101，返回 (path, 请求头, 多读到的字节)。

    不校验 Key（我们要的是"它把 Key 原样带过来了吗"这个观测，不是兼容性）。
    但**校验重复头**：`Connection`/`Upgrade`/`Sec-WebSocket-Key`/`Sec-WebSocket-Version`
    任一条出现两次就回 426 而不是 101。这条严度是照 websockets 16.1 的实测行为钉的：
    它单份 Upgrade 给 101、双份 Upgrade 给 426（重复 Connection 反而容忍）—— 假上游若比
    真上游宽松，转发侧多写一条头这种事就只能在生产里暴露（本轮就是这么撞上的）。
    返回的 rest 很要紧：转发是字节泵，客户端的握手请求和第一帧很可能在同一个 TCP 段里，
    上游一次 recv 就会把两者都读到 —— 丢了 rest 就等于把首帧吃掉。
    """
    head = bytearray()
    while b"\r\n\r\n" not in head:
        more = sock.recv(4096)
        if not more:
            raise ConnectionError("客户端在握手完成前关闭")
        head += more
        if len(head) > 65536:
            raise ConnectionError("握手请求过大")
    block, _, rest = bytes(head).partition(b"\r\n\r\n")
    lines = block.decode("latin-1", "replace").split("\r\n")
    # 请求行是 "GET / HTTP/1.1" —— 只取中间那段，别把版本号当成路径的一部分
    parts = lines[0].split(" ")
    path = (parts[1] if len(parts) > 1 else "/").split("?")[0]
    headers = RequestHeaders()
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers.add(name.strip().lower(), value.strip())
    key = headers.get("sec-websocket-key", "")
    dupes = sorted(set(headers.duplicates) & {
        "connection", "upgrade", "sec-websocket-key", "sec-websocket-version"})
    if dupes:
        sock.sendall(
            ("HTTP/1.1 426 Upgrade Required\r\n"
             "Connection: close\r\n"
             f"X-Duplicate-Headers: {', '.join(dupes)}\r\n\r\n").encode("latin-1"))
        sock.close()
        raise HandshakeError(f"上游因为重复的请求头拒了握手：{dupes}")
    reply = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_for(key) if key else ''}\r\n\r\n"
    )
    sock.sendall(reply.encode("latin-1"))
    return path, headers, rest


def connect(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock
