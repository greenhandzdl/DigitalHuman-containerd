#!/usr/bin/env python3
"""FunASR 服务（containerd/asr/server.py）的协议测试 —— 不加载 torch，秒级。

跑在 dh-funasr:local 里，但用 `ASR_FAKE_MODEL=1` 让 server 换上那个假模型：
`generate()` 返回 "字<这段音频第一个样点的值>"。于是"识别出了什么"变成完全确定的，
而**协议本身**（切块、累计、final 唯一、收工、边界）走的还是生产那份代码 —— 包括
`websockets.serve` 真的在解掩码帧这条。为这个多等一次 torch 加载不值。

四条判据是直接照伙伴方那份 server.py 的偏差写的，它们在原版上都会红：
  * 4 累计前缀单调增长（原版每块只回那一秒的分片，而 App.vue 是**覆盖**输入框）
  * 5 final 的文本是整段（原版同上）
  * 7 零字节录音也必发一个 final（原版只在有文本时发帧 → 录音按钮永远停不下来）
  * 11 超 FUNASR_MAX_SECONDS 强制收工（原版没有任何上界，按着不放就是无限吃 CPU）

判据：
  1 服务起得来：ready 文件 + ASR READY 日志 + 握手 101
  2 每 32000 字节（=1 秒）出一帧 partial，不多不少
  3 每帧都是 JSON 文本帧 {"text": str, "is_final": bool}
  4 partial 之间前缀单调增长，且内容就是各段拼起来的整段
  5 StopTranscription → 有且只有一个 final，文本等于全部已识别内容
  6 不满一块的尾巴也会被识别并进 final
  7 一个字节都没喂也照样有 final（text 为空串）
  8 无法解析的文本帧只是被忽略，这一路之后照常出字
  9 StartTranscription（Fay 那半条方言）接受并忽略
 10 两路并发各说各的，final 互不含对方的段
 11 MAX_SECONDS=2 的实例：不等到 Stop 就自己收工
 12 收工后这条连接由服务端关闭，而服务照常接下一条
 13 负面自检：把 final 改回"没字就不发"（原版写法）→ 判据 7 必须变红

客户端帧全部走 probes/wsutil.py 手写掩码帧 —— 那是浏览器真实的发法，
也正是 `websockets` 那个库会挑刺的地方。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wsutil as W  # noqa: E402

SERVER = os.environ.get("ASR_SERVER_PATH") or "/app/server.py"
CHUNK = 32000                      # 16kHz × int16 × 1 秒，服务端切块尺寸
BROWSER_FRAME = 8192               # ScriptProcessor(4096,1,1)：浏览器每次发这么多字节
PORT_MAIN = 19921
PORT_LIMIT = 19922

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def pcm(seconds: float, start: int = 1) -> bytes:
    """造 n 秒静音，但每 16000 样点（=32000 字节=1 秒）的开头放一个递增样点。

    假模型只读每段的第一个样点，所以"第 k 块"会稳定地识别成 字(start+k) ——
    累计文本因此是可以逐字断言的。
    """
    import numpy as np

    samples = int(16000 * seconds)
    data = np.zeros(samples, dtype="<i2")
    for k in range((samples + 15999) // 16000):
        data[k * 16000] = start + k
    return data.tobytes()


# --------------------------------------------------------------------------- 被测服务
class AsrServer:
    def __init__(self, port: int, fake: bool = True, path: str | None = None, **extra: str) -> None:
        self.port = port
        self.path = path or SERVER
        self.ready = f"/tmp/asr-test-{port}.ready"
        self.log_path = f"/tmp/asr-test-{port}.log"
        try:
            os.unlink(self.ready)
        except OSError:
            pass
        env = {**os.environ, "ASR_FAKE_MODEL": "1" if fake else "", "FUNASR_HOST": "127.0.0.1",
               "FUNASR_PORT": str(port), "ASR_READY_FILE": self.ready,
               "PYTHONUNBUFFERED": "1", **extra}
        self.log = open(self.log_path, "wb")
        self.proc = subprocess.Popen([sys.executable, self.path], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout: float = 60) -> bool:
        """就绪 = ready 文件在 **且** 端口真能连上。

        只认文件会在"写完文件再去 bind"的实现上稳定撞车（判据 1 第一次红就是这么来的：
        假模型秒起，connect 抢在 listen 之前）。现在的 server.py 把文件写在 bind 之后，
        这一层是让它自己的判据自己成立，不依赖被测实现的顺序。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.ready):
                try:
                    probe = W.connect("127.0.0.1", self.port, timeout=2)
                    probe.close()
                    return True
                except OSError:
                    pass
            if self.proc.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def text(self) -> str:
        try:
            return open(self.log_path, encoding="utf-8", errors="replace").read()
        except OSError:
            return ""

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


class Session:
    """一路客户端：握手、按浏览器的节奏喂 PCM、收帧。"""

    def __init__(self, port: int) -> None:
        self.sock = W.connect("127.0.0.1", port, timeout=20)
        self.status, self.headers, self.reader = W.client_handshake(
            self.sock, "/", f"127.0.0.1:{port}")

    def send(self, data: bytes, opcode: int = W.OP_BINARY) -> None:
        for i in range(0, len(data), BROWSER_FRAME):
            self.sock.sendall(W.encode_frame(data[i:i + BROWSER_FRAME], opcode=opcode, mask=True))

    def feed(self, data: bytes) -> None:
        self.send(data)

    def control(self, payload: dict | str) -> None:
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.sock.sendall(W.encode_frame(raw.encode(), opcode=W.OP_TEXT, mask=True))

    def stop(self) -> None:
        self.control({"state": "StopTranscription"})

    def frames(self, want_final: bool = True, timeout: float = 10.0) -> list[dict]:
        """读到 is_final 或超时为止。partial 是流式的，所以只能靠超时收尾。"""
        out: list[dict] = []
        self.sock.settimeout(timeout)
        while True:
            try:
                op, payload = self.reader.frame()
            except (ConnectionError, OSError):
                return out
            if op != W.OP_TEXT:
                out.append({"_opcode": op, "_raw": payload})
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                out.append({"_bad": payload[:60].decode("utf-8", "replace")})
                continue
            out.append(obj)
            if want_final and obj.get("is_final"):
                return out

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- 判据
def run_checks(srv: AsrServer) -> None:
    # 1 起得来
    s = Session(srv.port)
    ok1 = s.status.endswith("101 Switching Protocols")
    check(ok1, "1 服务就绪且握手回 101",
          f"{s.status!r} ASR READY={'ASR READY' in srv.text()}")
    s.close()

    # 2/3/4/5 三秒音频：三帧 partial + 一帧 final，内容全是确定的
    s = Session(srv.port)
    s.feed(pcm(3.0))
    got = s.frames()
    partials = [f for f in got if f.get("is_final") is False]
    finals = [f for f in got if f.get("is_final") is True]
    ok2 = len(partials) == 3 and len(finals) == 0
    check(ok2, "2 每 32000 字节出一帧 partial（3 秒 → 恰好 3 帧）",
          f"partial={len(partials)} 提前 final={len(finals)}")
    shape_ok = all(set(f) == {"text", "is_final"} and isinstance(f["text"], str)
                   for f in partials)
    check(shape_ok, "3 每帧都是 {\"text\",\"is_final\"} 这一种 JSON 文本帧",
          f"形状全对={shape_ok} 首帧={partials[0] if partials else '无'}")
    want = ["字1", "字1字2", "字1字2字3"]
    prefix_ok = [p["text"] for p in partials] == want
    mono_ok = all(partials[i]["text"] in partials[i + 1]["text"] for i in range(len(partials) - 1))
    check(prefix_ok, "4 partial 是累计文本且逐帧前缀延长（原版回的是分片）",
          f"收到 {[p['text'] for p in partials]} 期望 {want}")
    s.stop()
    tail = s.frames(timeout=6)
    finals = [f for f in tail if f.get("is_final") is True]
    check(len(finals) == 1 and finals[0]["text"] == "字1字2字3",
          "5 StopTranscription → 有且只有一个 final，文本是整段",
          f"final={finals}")
    s.close()

    # 6 不满一块的尾巴
    s = Session(srv.port)
    s.feed(pcm(1.5))            # 32000 + 16000
    mid = s.frames(want_final=False, timeout=4)
    s.stop()
    end = s.frames(timeout=6)
    finals = [f for f in end if f.get("is_final") is True]
    check(len(mid) == 1 and len(finals) == 1 and finals[0]["text"] == "字1字2",
          "6 尾巴不足一块也会被识别并进 final",
          f"partial={len(mid)} final={finals}")
    s.close()

    # 7 零字节录音
    s = Session(srv.port)
    s.stop()
    empty = s.frames(timeout=6)
    finals = [f for f in empty if f.get("is_final") is True]
    check(len(finals) == 1 and finals[0]["text"] == "",
          "7 一个字节都没喂也必发一个 final（否则录音按钮停不下来）",
          f"收到 {empty}")
    s.close()

    # 8 非法文本帧只是被忽略
    s = Session(srv.port)
    s.control("这不是 JSON")
    s.feed(pcm(1.0))
    after = s.frames(want_final=False, timeout=5)
    s.stop()
    end = s.frames(timeout=6)
    finals = [f for f in end if f.get("is_final") is True]
    ok8 = (len(after) == 1 and after[0].get("text") == "字1"
           and len(finals) == 1 and finals[0]["text"] == "字1")
    check(ok8, "8 无法解析的文本帧被忽略，这一路之后照常出字",
          f"坏帧后 partial={after} final={finals}")
    s.close()

    # 9 StartTranscription（Fay 的方言）不应当成错误
    s = Session(srv.port)
    s.control({"state": "StartTranscription"})
    s.feed(pcm(1.0))
    mid = s.frames(want_final=False, timeout=5)
    s.stop()
    end = s.frames(timeout=6)
    ok9 = len(mid) == 1 and any(f.get("is_final") for f in end)
    check(ok9, "9 StartTranscription 接受并忽略（不报错、不断连）",
          f"partial={mid} 收工帧={[f for f in end if f.get('is_final')]}")
    s.close()

    # 10 两路并发各说各的
    a, b = Session(srv.port), Session(srv.port)
    a.feed(pcm(2.0, start=1))          # 字1 字2
    b.feed(pcm(2.0, start=50))         # 字50 字51
    a.stop(); b.stop()
    fa = [f for f in a.frames(timeout=8) if f.get("is_final")]
    fb = [f for f in b.frames(timeout=8) if f.get("is_final")]
    ok10 = (fa and fb and fa[0]["text"] == "字1字2" and fb[0]["text"] == "字50字51")
    check(ok10, "10 两路并发互不含对方的字（会话状态没串线）",
          f"甲={fa[:1]} 乙={fb[:1]}")
    a.close(); b.close()

    # 12 前面每条都在同一个服务上开关连接，这里只验它还在接新连接
    s = Session(srv.port)
    s.stop()
    ok12 = any(f.get("is_final") for f in s.frames(timeout=6))
    check(ok12, "12 上面那些收工之后服务照常接新连接", f"新会话正常收工={ok12}")
    s.close()


def run_limit_check(srv: AsrServer) -> None:
    """11 MAX_SECONDS=2 的那个实例：不等到 Stop 就该自己收工。"""
    s = Session(srv.port)
    s.feed(pcm(4.0))                   # 4 秒 > FUNASR_MAX_SECONDS=2
    s.sock.settimeout(15)
    got = []
    try:
        while True:
            op, payload = s.reader.frame()
            obj = json.loads(payload)
            got.append(obj)
            if obj.get("is_final"):
                break
    except (ConnectionError, OSError, json.JSONDecodeError):
        pass
    finals = [f for f in got if f.get("is_final")]
    ok = len(finals) == 1
    check(ok, "11 超过 FUNASR_MAX_SECONDS 时服务端自己收工（发且只发一个 final）",
          f"没发 Stop 就收到 {len(got)} 帧，final={finals}")
    s.close()


def negative_control() -> bool:
    """把 final 那步改回伙伴方原版的写法（没字就不发帧）：判据 7 必须变红。"""
    src = open(SERVER, encoding="utf-8").read()
    anchor = ('        self.frames_out += 1\n'
              '        log.info(\'ASR TEXT id=%d final "%s"\', self.sid, full)\n'
              '        await ws.send(json.dumps({"text": full, "is_final": True}, ensure_ascii=False))\n')
    broken_src = src.replace(anchor, ('        self.frames_out += 1\n'
                                      '        if full:      # 伙伴方原版：没识别出字就一帧都不发\n'
                                      '            log.info(\'ASR TEXT id=%d final "%s"\', self.sid, full)\n'
                                      '            await ws.send(json.dumps({"text": full, "is_final": True},\n'
                                      '                             ensure_ascii=False))\n'), 1)
    if broken_src == src:
        print("FAIL  13 负面自检没能改坏任何东西（替换锚点失效，这份自检已不可信）")
        return False
    path = "/tmp/asr_server_broken.py"
    open(path, "w", encoding="utf-8").write(broken_src)
    srv = AsrServer(PORT_MAIN + 2, path=path)
    try:
        ready = srv.wait_ready()
        mutated = False
        if ready:
            s = Session(srv.port)
            s.stop()
            got = s.frames(timeout=6)
            mutated = not any(f.get("is_final") for f in got)
            s.close()
        else:
            mutated = True   # 改坏到起不来，也算这条自检没白写
    finally:
        srv.close()
    print(f"{'PASS' if mutated else 'FAIL'}  13 负面自检：final 改回「没字就不发」后判据 7 确实撑不住"
          f"  —— 变红={mutated}", flush=True)
    return mutated


def main() -> int:
    if not os.path.isfile(SERVER):
        print(f"[asr-test] 找不到 {SERVER}（这组要跑在 dh-funasr:local 里）", file=sys.stderr)
        return 2
    limit = AsrServer(PORT_LIMIT, FUNASR_MAX_SECONDS="2", FUNASR_MAX_INFLIGHT="2")
    main_srv = AsrServer(PORT_MAIN)
    try:
        if not main_srv.wait_ready() or not limit.wait_ready():
            print("FAIL  1 服务就绪且握手回 101  —— 假模型都起不来，后面没法测", flush=True)
            print(main_srv.text()[-1500:], limit.text()[-500:], file=sys.stderr)
            return 1
        run_checks(main_srv)
        run_limit_check(limit)
    finally:
        main_srv.close()
        limit.close()
    red = negative_control()

    good = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS) + 1
    failed = total - good - (1 if red else 0)
    print(f"\n[asr-test] {total - failed}/{total} 通过"
          + ("" if failed == 0 else f"，{failed} 项失败")
          + ("" if red else "（负面自检没能把判据 7 弄红，这套判据是摆设）"))
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}  {detail}")
    return 0 if failed == 0 and red else 1


if __name__ == "__main__":
    sys.exit(main())
