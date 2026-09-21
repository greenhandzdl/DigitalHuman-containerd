#!/usr/bin/env python3
"""FunASR 流式识别服务 —— 说 CareEcho H5 那种方言的真后端。

线上协议（**逐字段照前端来，前端一个字都不改**，见 ../frontend/careecho-h5/src/api/funasr.js）：

    入：二进制帧 = 无 WAV 头的 PCM，int16 / 16kHz / 单声道（浏览器 ScriptProcessor 每 4096 样点一发）
        文本帧   = {"state":"StopTranscription"}（收工信号；StartTranscription 也接受并忽略）
    出：只有 JSON 文本帧 {"text": str, "is_final": bool}

为什么要有这个服务：H5 点麦克风按钮时连的是**页面同源**的 ws://<host>/funasr-ws，
而我们发出去的是 `npm run build` 的产物（Vite 的 dev proxy 只在 `npm run dev` 下生效），
所以那条 ws 需要有人接 —— 生产由前端外壳（frontend/carecho_web.py）同源转发进来，
只有 dev 档位才把它直接发布到宿主。

与伙伴方那台镜像里的 server.py 相比，协议一致，改了四处会真咬人的实现：

1. **识别不在事件循环里跑**。原版每个 1 秒音频块直接 `model.generate(...)`，
   那是同步 CPU 推理；一旦单块耗时超过 1 秒（paraformer-large 在满载机器上是常态），
   事件循环就被拖住 —— 连 `ping_interval=10` 的心跳都发不出去，浏览器于是把连接判死。
   现在推理走 `asyncio.to_thread`，心跳与收帧不受影响。
2. **回的是累计文本**。原版每块只回那一秒的分片，而 `App.vue:138-148` 是**每帧覆盖**输入框、
   只在 `is_final` 收工 —— 覆盖到最后一帧，用户看到的就是只剩下的那半句。
   现在 partial 与 final 都是"从第一个字到现在"的整段，因此每帧都是上一帧的前缀延长。
3. **final 一定发**。原版只在有文本时才发帧，于是一段没识别出字的录音永远等不到 `is_final`，
   录音按钮就停不下来。现在收工时必发且只发一个 final（哪怕是空串）。
4. **有边界**。音频按秒计价，所以对外部输入要有上界：单会话最长 FUNASR_MAX_SECONDS（默认 60s，
   超了直接强制收工），并发推理条数上限 FUNASR_MAX_INFLIGHT（默认 2，护住内存 ——
   模型本身约 2GB，这台机器可用内存约 8GB）。

日志是刻意做成可 grep 的单行的（ASR READY / ASR SESSION / ASR TEXT / ASR MODEL），
probes/asr_test.py 与 probes/asr_probe.py 的判据要能在容器日志里对上号。

用标准环境变量调：FUNASR_HOST FUNASR_PORT FUNASR_MODEL FUNASR_VAD_MODEL FUNASR_PUNC_MODEL
FUNASR_REVISION FUNASR_CHUNK_BYTES FUNASR_MAX_SECONDS FUNASR_MAX_INFLIGHT FUNASR_TORCH_THREADS
ASR_FAKE_MODEL ASR_READY_FILE
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
import wave

import numpy as np
import websockets

HOST = os.environ.get("FUNASR_HOST", "0.0.0.0")
PORT = int(os.environ.get("FUNASR_PORT", "10095"))
# 三个模型名都用 `or` 而不是 `.get(k, default)`：compose 里 `${FUNASR_MODEL:-...}` 这类
# 写法一旦被谁改成 `${FUNASR_MODEL:-}`，传进来的就是**空字符串**而不是"没这个键"，
# .get 的默认值不会生效，funasr 会拿着它去 os.path.exists 崩在自己的下载分支里。
# 空 = 没配 = 用默认，这个语义在这里更合适。
MODEL_NAME = os.environ.get("FUNASR_MODEL") or "paraformer-zh"
VAD_MODEL = os.environ.get("FUNASR_VAD_MODEL") or "fsmn-vad"
PUNC_MODEL = os.environ.get("FUNASR_PUNC_MODEL") or "ct-punc-c"
REVISION = os.environ.get("FUNASR_REVISION", "v2.0.4")
# 16kHz × int16 × 单声道 = 32000 字节/秒。切块尺寸就是"多久出一次 partial"。
CHUNK_BYTES = int(os.environ.get("FUNASR_CHUNK_BYTES", "32000"))
MAX_SECONDS = int(os.environ.get("FUNASR_MAX_SECONDS", "60"))
MAX_INFLIGHT = int(os.environ.get("FUNASR_MAX_INFLIGHT", "2"))
TORCH_THREADS = os.environ.get("FUNASR_TORCH_THREADS", "4")
FAKE_MODEL = os.environ.get("ASR_FAKE_MODEL", "") not in ("", "0", "false")
READY_FILE = os.environ.get("ASR_READY_FILE", "/tmp/asr.ready")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
log = logging.getLogger("asr")


class FakeModel:
    """不加载 torch 的假模型：把送进来的那段音频**第一个样点值**当成识别结果。

    存在的理由只有一个 —— probes/asr_test.py 要能在任何机器上秒级判掉协议本身
    （切块对齐、累计前缀、final 唯一、多会话不串线），那几件事与 torch 无关。
    从 wav 里读样点（而不是只看字节数）是为了把 WAV 头那几行也一起验掉：
    采样率/位宽/声道数写错，这里读回来的第一个样点就不是发进去的那个。
    """

    def generate(self, input: str, is_final: bool = False, sentence_timestamp: bool = False):
        with wave.open(input, "rb") as wf:
            frames = wf.readframes(1)
        value = int(np.frombuffer(frames, dtype="<i2")[0]) if len(frames) == 2 else 0
        return [{"text": "字%d" % value}]


def load_model():
    if FAKE_MODEL:
        return FakeModel()
    import torch

    torch.set_num_threads(int(TORCH_THREADS))
    from funasr import AutoModel

    # 三个模型一起 load：paraformer 出字、fsmn-vad 断句、ct-punc 补标点。
    # 少了后两个，回来的是一串没有停顿的空格字，前端展示上等于没识别。
    return AutoModel(
        model=MODEL_NAME,
        model_revision=REVISION,
        vad_model=VAD_MODEL,
        vad_model_revision=REVISION,
        punc_model=PUNC_MODEL,
        punc_model_revision=REVISION,
        disable_update=True,
        device="cpu",
    )


MODEL_LOADER = load_model
_INFLIGHT = asyncio.Semaphore(MAX_INFLIGHT)


def cache_bytes() -> int:
    """MODELSCOPE_CACHE 里现有字节数。加载前后各量一次，差值就是「这次到底下没下载」。

    为什么不看目录名里有没有模型名：paraformer-zh 是个别名，缓存里落的是
    iic--paraformer-large-..._pytorch/snapshots/v2.0.4 这种全名，按别名匹配必然假阴。
    """
    root = os.environ.get("MODELSCOPE_CACHE") or os.path.expanduser("~/.cache/modelscope")
    total = 0
    for dirpath, _, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


async def recognize(model, pcm: bytes, is_final: bool) -> str:
    """把一段裸 PCM 写成临时 wav 再识别。推理走 to_thread，事件循环不欠心跳。"""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm)
        async with _INFLIGHT:
            res = await asyncio.to_thread(
                model.generate, input=path, is_final=is_final, sentence_timestamp=False
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not res:
        return ""
    text = (res[0] or {}).get("text", "")
    return text.strip()


class Session:
    """一路连接 = 一次录音。parts 是已定稿的分片，合起来就是回给前端的累计文本。"""

    def __init__(self, sid: int, model) -> None:
        self.sid = sid
        self.model = model
        self.pending = bytearray()
        self.parts: list[str] = []
        self.bytes_in = 0
        self.frames_out = 0
        self.closed = False

    @property
    def sofar(self) -> str:
        return "".join(self.parts)

    async def feed(self, pcm: bytes, ws) -> None:
        if self.closed:  # 被 FUNASR_MAX_SECONDS 强制收工之后，这一路就只等连接关了
            return
        self.pending += pcm
        self.bytes_in += len(pcm)
        while len(self.pending) >= CHUNK_BYTES:
            chunk = bytes(self.pending[:CHUNK_BYTES])
            del self.pending[:CHUNK_BYTES]
            await self._emit(chunk, ws, is_final=False)
        if MAX_SECONDS and self.bytes_in > CHUNK_BYTES * MAX_SECONDS:
            log.warning("ASR SESSION id=%d 超过 FUNASR_MAX_SECONDS=%d，强制收工", self.sid, MAX_SECONDS)
            await self.finish(ws)

    async def _emit(self, pcm: bytes, ws, is_final: bool) -> None:
        text = await recognize(self.model, pcm, is_final)
        if text:
            self.parts.append(text)
        if is_final:
            return  # final 由 finish() 统一发，保证"有且只有一个"
        self.frames_out += 1
        full = self.sofar
        log.info('ASR TEXT id=%d partial "%s"', self.sid, full)
        await ws.send(json.dumps({"text": full, "is_final": False}, ensure_ascii=False))

    async def finish(self, ws) -> None:
        """收尾：把不满一秒的尾巴识别掉，然后**必发**一个 final（哪怕整句是空的）。"""
        if self.closed:
            return
        self.closed = True
        tail = bytes(self.pending)
        del self.pending[:]
        if tail:
            text = await recognize(self.model, tail, is_final=True)
            if text:
                self.parts.append(text)
        full = self.sofar
        self.frames_out += 1
        log.info('ASR TEXT id=%d final "%s"', self.sid, full)
        await ws.send(json.dumps({"text": full, "is_final": True}, ensure_ascii=False))
        log.info("ASR SESSION id=%d bytes=%d frames=%d finals=1", self.sid, self.bytes_in, self.frames_out)


async def handle(websocket, path=None) -> None:
    """path 给了默认值：websockets 14+ 的 handler 只收一个参数，13.x 收两个。"""
    sid = id(websocket)
    session = Session(sid, LOADED)
    log.info("ASR SESSION id=%d open remote=%s", sid, getattr(websocket, "remote_address", "?"))
    try:
        async for message in websocket:
            if isinstance(message, (bytes, bytearray)):
                await session.feed(bytes(message), websocket)
                continue
            try:
                state = json.loads(message).get("state")
            except (ValueError, AttributeError):
                log.warning("ASR SESSION id=%d 收到无法解析的文本帧，忽略", sid)
                continue
            if state == "StopTranscription":
                await session.finish(websocket)
                return
            # StartTranscription（Fay 那半条方言）与其它 state：接受并忽略，不当错误。
            log.info("ASR SESSION id=%d state=%s（忽略）", sid, state)
        await session.finish(websocket)  # 浏览器直接关页面：也要给它一个 final 的机会
    except websockets.exceptions.ConnectionClosed:
        log.info("ASR SESSION id=%d 客户端先关了", sid)
    except Exception:  # noqa: BLE001 - 一路坏连接不能带走整个服务
        log.exception("ASR SESSION id=%d 异常", sid)


# 加载完的那个 AutoModel 实例。**不要**也叫 MODEL：上面那个 MODEL_NAME 是模型名（字符串），
# 两者共用一个名字会让 main 的赋值把名字冲成 None，而崩溃点在 funasr 里面，看不出来。
LOADED = None


async def main() -> None:
    global LOADED
    ready = os.environ.get("ASR_READY_FILE", READY_FILE)
    try:
        os.unlink(ready)
    except OSError:
        pass
    t0 = time.monotonic()
    before = cache_bytes()
    LOADED = await asyncio.to_thread(MODEL_LOADER)
    after = cache_bytes()
    secs = time.monotonic() - t0
    # src=hub 表示这一启动真的从魔搭下载了模型（缓存没命中或冷启动）；src=local 是命中缓存卷。
    log.info("ASR MODEL src=%s cache=%s bytes=%d->%d fake=%s",
             "hub" if after > before else "local",
             os.environ.get("MODELSCOPE_CACHE", "~/.cache/modelscope"), before, after, FAKE_MODEL)
    log.info("ASR READY secs=%.1f model=%s chunk=%dB max=%ds inflight=%s",
             secs, LOADED.__class__.__name__, CHUNK_BYTES, MAX_SECONDS, MAX_INFLIGHT)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # PID 1 收 SIGTERM 直接死会让退出码是 143，看着像崩溃；这里是本栈的既定习惯
    # （fay-lite 那条「优雅停止退出码 0」就是同一件事），换成干净的 0。
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(handle, HOST, PORT, ping_interval=10, ping_timeout=30):
        # 就绪文件写在 bind **之后**：compose 的健康检查和 probes/asr_test.py 都拿它当
        # "可以打了"的唯一凭据，先写后 bind 会让那两者连上一个还没人听的端口 ——
        # asr-test 判据 1 第一次就是这么红的（假模型秒起，于是稳定撞上那几毫秒）。
        with open(ready, "w", encoding="utf-8") as fh:
            fh.write("ready\n")
        log.info("ASR LISTEN ws://%s:%d  入=PCM16/16k/mono  出={\"text\",\"is_final\"}", HOST, PORT)
        await stop.wait()
    log.info("ASR STOPPED 优雅退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
