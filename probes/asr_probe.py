#!/usr/bin/env python3
"""FunASR 真链路探针：无麦、真模型、真语音（三组 ASR 测试件里的最后一组）。

与 probes/asr_test.py 的分工是刻意的：那组用 `ASR_FAKE_MODEL=1` 的假模型把**协议**
钉死（确定性、秒级、不碰 torch）；这一组对着**真 paraformer** 放**真语音**，判的是
"这台机器上麦克风那条链装完模型之后真的出字"。代价是它要 CPU 推理，所以 run.sh 把它
排在全套最后，且慢与降级都不等于坏。

跑在 dh-fay:local 而不是 dh-funasr:local，是因为下面那条"已知原文"判据要合成 ——
`to_sample` 走的是被测栈里那份 `tts/ms_tts_sdk`（与 fay_probe 的 TTS 判据同一个音色解析、
同一条出网依赖），ASR 镜像里故意不带 edge_tts。被测对象仍然是那个真服务：客户端只需要
stdlib + numpy，握手和帧都走 probes/wsutil.py 手造的**带掩码**帧，不引 websockets/ffmpeg。

音频从哪来（两条，按可用性叠加）：
  * `fay/samples/*.wav` —— 上游仓库里带的那段真人语音（15.26s、16kHz/单声道/16bit，
    正好是 ASR 的原生格式，连重采样都不需要）。它跟着 clone 一起来，不依赖网络、
    不依赖这台机器说过什么话，所以主判据全压在它身上。
    **不挂运行期那个 `fay-samples` 卷**：fay/main.py:181 的 `__clear_samples()` 每次
    启动都把 `./samples` 清空（run #32 里那 6 段 edge_tts 产物就在 dh-fay 被重建的瞬间
    没了），拿它当测试输入等于埋一个"每轮必空"的依赖。
  * 探针自己合成一句知道原文的话（`KNOWN_TEXT`，走被测栈里那份 `tts/ms_tts_sdk`）。
    仓库那份的文本我们**不知道**，所以它只够判协议与"认出了中文"，判不了"认得对" ——
    这半条补的就是"认得对"。合成不成只影响判据 7，并点名是"出网那半条"。

判据（每条都可能变红）：
  1 服务在听：TCP 连得上 + WS 握手 101        —— 连不上按环境降级（模型还没加载完 / OOM）
  2 有可放的音频（至少一段 ≥1.2s 的 16bit wav）
  3 边说边出字：StopTranscription **之前**就收到过 partial（流式的定义本身）
  4 收工之前没有过早的 is_final，且每段**恰好一个** final
     （浏览器 App.vue 只在 is_final 收手：过早 = 录音停不下来 / 永远停不下来）
  5 final 含 CJK（真语音认不出全 ASCII 是缺陷）
  6 累计单调：后一帧以前一帧为前缀，final 又以最后一个 partial 为前缀
     —— 这条的负面自检在 asr_test.py（那边确定性、这边不重复花一次真推理）
  7 已知原文那句话认出了开头两个字（挡"回一句固定中文"的实现）
  8 不同音频的 final 互不相同（同上，另一半边）
  9 字数与时长同量级（0.5~15 字/秒）：挡"整段只认出一个字"和"凭空刷一大段"

用法：python asr_probe.py [--host funasr] [--port 10095] [--samples /samples]
退出码 = FAIL 条数（SKIP 不算失败，但每条的成因都逐行列出）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import socket
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wsutil as W  # noqa: E402  —— 同目录兄弟模块，与外壳那两组测试共用一份帧构造器

TARGET_RATE = 16000
# 浏览器侧 ScriptProcessor(4096,1,1) 每 4096 样点一发 = 8192 字节。照它发，探针才是
# 那个前端的行为等价物，而不是"把整段音频一次性塞进去的脚本"。
BROWSER_FRAME = 8192
BYTES_PER_SECOND = TARGET_RATE * 2
# 太短的样本凑不满第一个完整块（CHUNK_BYTES=32000），partial 必然为 0 ——
# 那是样本的问题，不是服务的问题，所以这类文件直接不参与判据。
MIN_SECONDS = 1.2
# 关键词取开头两个字：edge_tts 一句 ~4.5 字/秒，头两个字稳落在第一个 1 秒块里，
# 不会被切块边界劈开（换成句子中后段的词，判据就会随机红）。
KNOWN_TEXT = "探针麦克风链路自检"
CJK = re.compile(r"[\u4e00-\u9fff]")

RESULTS: list[tuple[bool | None, str, str]] = []   # (ok, name, detail)，ok=None → SKIP
_LABELS = {True: "PASS", False: "FAIL", None: "SKIP"}
ENV_SKIP_MARK = "按环境降级"
# 服务不在线时置值：收尾那行要说清"这一堆 SKIP 是同一个环境原因"，而不是逐条打太极。
DEGRADED_ASR_HOST: str | None = None


def record(ok: bool | None, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"{_LABELS[ok]}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def degrade(name: str, why: str) -> None:
    """判不了 ≠ 判不过：环境原因（服务没就绪、机器没有样本）一律 SKIP 并写清缺什么。"""
    record(None, name, f"{why}｜{ENV_SKIP_MARK}")


# --------------------------------------------------------------------------- 音频侧
def load_pcm(path: str) -> tuple[bytes, float] | None:
    """wav → (16k/单声道/int16 裸 PCM, 秒)。重采样是线性的，够用且可复算。"""
    with wave.open(path, "rb") as wf:
        rate, ch, width, n = (wf.getframerate(), wf.getnchannels(),
                             wf.getsampwidth(), wf.getnframes())
        raw = wf.readframes(n)
    if width != 2 or n == 0:
        return None
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    if rate != TARGET_RATE:
        out = int(round(samples.size * TARGET_RATE / rate))
        samples = np.interp(np.linspace(0.0, samples.size - 1.0, out, dtype=np.float64),
                            np.arange(samples.size, dtype=np.float64), samples)
    pcm = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    return pcm, len(pcm) / BYTES_PER_SECOND


def discover(samples_dir: str, limit: int) -> list[tuple[str, bytes, float]]:
    """卷里挑 limit 段能放的 wav，按名字倒序（伙伴方那份是 sample-<毫秒>，新的在前）。"""
    found: list[tuple[str, bytes, float]] = []
    skipped: list[str] = []
    try:
        names = sorted(os.listdir(samples_dir), reverse=True)
    except OSError as exc:
        print(f"（样本目录读不了：{exc}）", file=sys.stderr)
        return found
    for name in names:
        if not name.lower().endswith(".wav"):
            continue
        path = os.path.join(samples_dir, name)
        try:
            loaded = load_pcm(path)
        except (EOFError, wave.Error, OSError):
            skipped.append(f"{name}(读不出)")
            continue
        if loaded is None:
            skipped.append(f"{name}(非 16bit)")
            continue
        pcm, secs = loaded
        if secs < MIN_SECONDS:
            skipped.append(f"{name}({secs:.2f}s)")
            continue
        found.append((name, pcm, secs))
        if len(found) >= limit:
            break
    if skipped:
        print("（跳过：" + "、".join(skipped) + "）", flush=True)
    return found


def synthesize() -> tuple[bytes, float] | None:
    """用被测栈那份 edge_tts 合成 KNOWN_TEXT；任何一步不成都返回 None（只影响判据 7）。"""
    try:
        sys.path.insert(0, os.getcwd())
        from utils import config_util as cfg

        cfg.load_config()
        from tts.ms_tts_sdk import Speech

        path = Speech().to_sample(KNOWN_TEXT, "cheerful")
        if not isinstance(path, str) or not os.path.exists(path):
            print(f"（合成没落地：to_sample 返回 {path!r}）", file=sys.stderr)
            return None
        loaded = load_pcm(path)
        os.remove(path)
        for extra in (path.rsplit(".", 1)[0] + ".mp3",):
            try:
                os.remove(extra)
            except OSError:
                pass
        return loaded
    except Exception as exc:               # noqa: BLE001 - 这半条是可选的，不能带崩主判据
        print(f"（合成不可用：{type(exc).__name__}: {exc}）", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- 客户端
class Session:
    """一次连接 = 浏览器按一次麦克风。边推边收，推完发 StopTranscription 再等 final。"""

    def __init__(self, host: str, port: int, path: str, timeout: float) -> None:
        self.timeout = timeout
        self.sock = W.connect(host, port, timeout=min(10.0, timeout))
        try:
            _, _, self.reader = W.client_handshake(self.sock, path, f"{host}:{port}")
        except Exception:
            self.sock.close()
            raise
        self.sock.settimeout(timeout)

    def _pump(self, until: float) -> tuple[list[tuple[int, bytes]], bool]:
        """取走 until 之前能取到的完整帧 → (帧, 对端是否已关)。

        两处都是踩过才写下来的：
        * 只收**完整**帧，没收完的留在 Reader 缓冲区里等下一把 —— recv 很可能只带回
          半帧，把它当"没消息"扔掉就会永远拼不出那一帧。
        * 到期之后仍要再 select(0) 捞一把内核缓冲区。只看"用户态缓冲区里有没有完整帧"
          是不够的：帧一直躺在内核里，于是 15 帧 partial 全被算到收工之后，
          判据 3 稳定判成 0 次（run #32 第一次就是这么红的）。
        """
        frames: list[tuple[int, bytes]] = []
        expired = 0
        while True:
            while self.reader.has_frame():
                frames.append(self.reader.frame())
            left = until - time.monotonic()
            if left > 0:
                ready, _, _ = select.select([self.sock], [], [], min(0.25, left))
                if not ready:
                    continue
            else:
                expired += 1
                if expired > 4 or not select.select([self.sock], [], [], 0)[0]:
                    return frames, False
            try:
                more = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return frames, True
            if not more:
                return frames, True
            self.reader.buf += more

    def stream(self, pcm: bytes) -> dict:
        """按浏览器节奏推 PCM 并同步收帧；收工信号之后继续等那一个 final。"""
        partials: list[str] = []
        finals: list[str] = []
        malformed: list[str] = []
        early_finals = partials_before = control = 0
        eof = False

        def take(items: list[tuple[int, bytes]], before_stop: bool) -> None:
            nonlocal early_finals, partials_before, eof, control
            for opcode, payload in items:
                if opcode in (W.OP_PING, W.OP_PONG):
                    # websockets 库自己回 pong，我们不碰：这些帧是服务端 ping_interval=10
                    # 活着的证据，不是畸形帧 —— 曾把它记进 malformed，判据 4 的细节里
                    # 混着一句「非 JSON 帧：opcode=9」，看着像缺陷其实是在报心跳。
                    control += 1
                    continue
                if opcode == W.OP_CLOSE:
                    eof = True
                    continue
                if opcode != W.OP_TEXT:
                    malformed.append(f"opcode={opcode}")
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    malformed.append(repr(payload[:24]))
                    continue
                if not isinstance(obj, dict) or "text" not in obj:
                    malformed.append(str(obj)[:24])
                    continue
                if obj.get("is_final"):
                    finals.append(str(obj["text"]))
                    if before_stop:
                        early_finals += 1
                else:
                    partials.append(str(obj["text"]))
                    if before_stop:
                        partials_before += 1

        t_send = time.monotonic()
        for off in range(0, len(pcm), BROWSER_FRAME):
            self.sock.sendall(W.encode_frame(pcm[off:off + BROWSER_FRAME], W.OP_BINARY, mask=True))
            # 0 预算 = 只捞此刻已经到了的：partial 是不是"说话途中"来的，全靠这一捞
            frames, eof = self._pump(time.monotonic())
            take(frames, before_stop=True)
            if eof:
                break
            due = t_send + (off + BROWSER_FRAME) / BYTES_PER_SECOND
            nap = due - time.monotonic()
            if nap > 0:
                time.sleep(nap)
        stop_at = time.monotonic()
        self.sock.sendall(W.encode_frame(json.dumps({"state": "StopTranscription"}).encode(),
                                        W.OP_TEXT, mask=True))
        stop_deadline = stop_at + self.timeout
        while not finals and not eof and time.monotonic() < stop_deadline:
            frames, eof = self._pump(min(stop_deadline, time.monotonic() + 0.5))
            take(frames, before_stop=False)
        self.close()
        return {"partials": partials, "finals": finals, "malformed": malformed,
                "early_finals": early_finals, "partials_before": partials_before,
                "control": control,
                "eof": eof, "secs": len(pcm) / BYTES_PER_SECOND,
                "wall": time.monotonic() - t_send, "stop_wait": time.monotonic() - stop_at}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def probe_alive(host: str, port: int, path: str) -> tuple[bool, str]:
    """判据 1。分得细是因为两种"不在线"的处置相反：没监听 = 环境问题（降级），
    有人应答但不是 WS 101 = 真故障（FAIL）。"""
    try:
        session = Session(host, port, path, 10.0)
    except W.HandshakeError as exc:
        return False, f"handshake:{exc.status_line or exc}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    session.close()
    return True, ""


# --------------------------------------------------------------------------- 判据
def run(host: str, port: int, path: str, units: list[tuple[str, bytes, float]],
        timeout: float) -> None:
    """units 每一项都跑一遍，再按判据聚合（一段过不掩盖另一段，红也把名字带出来）。"""
    results: list[dict] = []
    for label, pcm, secs in units:
        session = Session(host, port, path, timeout)
        got = session.stream(pcm)
        got["label"] = label
        results.append(got)
        print(f"    · {label} {secs:.2f}s 音频 / {got['wall']:.1f}s 推流 + "
              f"{got['stop_wait']:.1f}s 收尾 → {len(got['partials'])} partial, "
              f"final={got['finals'][:1]}", flush=True)

    record(all(r["partials_before"] for r in results),
           "3 说话过程中就收到过 partial（不是收工才一次性回）",
           "、".join(f"{r['label']}:{r['partials_before']}" for r in results)
           + "（服务端按 CHUNK_BYTES=32000B 切块 = 每 1 秒音频一帧，所以边说边出字是判得出的）")

    late = [r["label"] for r in results if r["early_finals"]]
    exact = [f"{r['label']}:{len(r['finals'])}" for r in results]
    ok4 = not late and all(len(r["finals"]) == 1 for r in results)
    bad_shape = [f"{r['label']}:{m}" for r in results for m in r["malformed"][:2]]
    dropped = [r["label"] for r in results if r["eof"] and not r["finals"]]
    record(ok4, "4 每段恰好一个 is_final，且没有过早的 final",
           "、".join(exact)
           + f"（服务端心跳 ping 共 {sum(r['control'] for r in results)} 帧，不算数据帧）"
           + (f"｜收工前就来了 final：{'、'.join(late)}" if late else "")
           + (f"｜没等到 final 连接就被关了：{'、'.join(dropped)}" if dropped else "")
           + (f"｜非 JSON 数据帧：{'、'.join(bad_shape)}" if bad_shape else ""))

    finals = [(r["label"], "".join(r["finals"])) for r in results]
    empty = [label for label, text in finals if not text]
    record(all(text and CJK.search(text) for _, text in finals),
           "5 final 文本里真的有中文",
           "；".join(f"{label} {text[:24]!r}" for label, text in finals)
           + (f"｜空文本：{'、'.join(empty)}" if empty else ""))

    broken = []
    for r in results:
        chain = list(r["partials"]) + [r["finals"][0] if r["finals"] else ""]
        for a, b in zip(chain, chain[1:]):
            if not b.startswith(a):
                broken.append(f"{r['label']}: {a[:12]!r} → {b[:12]!r}")
                break
    record(not broken, "6 累计文本单调（后一帧以前一帧为前缀）",
           "；".join(broken) if broken else f"{len(results)} 段的前缀链都成立")

    known = [(label, text) for label, text in finals if label.endswith("|known")]
    if known:
        keyword = CJK.findall(KNOWN_TEXT)[:2]
        record("".join(keyword) in known[0][1],
               f"7 已知原文那句话认出了开头 {''.join(keyword)!r}",
               f"识别为 {known[0][1]!r}（原文 {KNOWN_TEXT!r}）")
    else:
        record(None, f"7 已知原文那句话认出了开头 {''.join(CJK.findall(KNOWN_TEXT))[:2]!r}",
               "本轮没有合成成功的音频：这半条依赖出网到 speech.platform.bing.com"
               "（与 fay_probe 的 TTS 判据同一个原因），主判据不依赖它")

    texts = [text for _, text in finals]
    if len(texts) >= 2:
        record(len(set(texts)) == len(texts), "8 不同音频的 final 互不相同（不是固定回一句）",
               f"{len(set(texts))} 种文本 / {len(texts)} 段")
    else:
        record(None, "8 不同音频的 final 互不相同（不是固定回一句）",
               f"只有一段音频（卷里样本 + 合成 = {len(texts)}），无从比较")

    rates = [(r["label"], len(CJK.findall(text)) / r["secs"])
             for r, (label, text) in zip(results, finals)]
    bad = [f"{label}：{rate:.1f} 字/秒" for label, rate in rates if not 0.5 <= rate <= 15.0]
    record(not bad, "9 字数与时长同量级（0.5~15 字/秒）",
           "；".join(bad) if bad else "、".join(f"{rate:.1f}" for _, rate in rates))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASR_PROBE_HOST", "funasr"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ASR_PROBE_PORT", "10095")))
    ap.add_argument("--path", default=os.environ.get("ASR_PROBE_PATH", "/"))
    ap.add_argument("--samples", default=os.environ.get("ASR_PROBE_SAMPLES", "/samples"))
    ap.add_argument("--limit", type=int, default=2, help="从卷里挑几段真语音")
    ap.add_argument("--timeout", type=float, default=120.0, help="单段等 final 的上限（秒）")
    ap.add_argument("--no-synth", action="store_true", help="不打外网，跳过已知原文那条")
    args = ap.parse_args()

    name1 = f"1 FunASR 在听且 WS 握得上手 ws://{args.host}:{args.port}{args.path}"
    alive, why = probe_alive(args.host, args.port, args.path)
    if alive:
        record(True, name1)
    elif "handshake:" in why:
        record(False, name1, f"{why}｜端口有人应答但不是 101 —— 同一个口被别的东西占了，"
                             "或者 server.py 改了线上方言")
        return finish()
    else:
        global DEGRADED_ASR_HOST
        DEGRADED_ASR_HOST = f"{args.host}:{args.port} 连不上（{why}）"
        degrade(name1, DEGRADED_ASR_HOST + "｜模型还在下载（首启 1.3GB）、容器没起，"
                                          "或这台机器还没 build funasr 镜像")
        for name in ("2 有可放的音频（≥1.2s 的 16bit wav）",
                     "3 说话过程中就收到过 partial（不是收工才一次性回）",
                     "4 每段恰好一个 is_final，且没有过早的 final",
                     "5 final 文本里真的有中文",
                     "6 累计文本单调（后一帧以前一帧为前缀）",
                     "7 已知原文那句话认出了关键词",
                     "8 不同音频的 final 互不相同（不是固定回一句）",
                     "9 字数与时长同量级（0.5~15 字/秒）"):
            degrade(name, "服务不可用，后面每条都判不了")
        return finish()

    units = discover(args.samples, args.limit)
    synth = None if args.no_synth else synthesize()
    if synth:
        # 标 |known：判据 7 靠这个后缀认出"这一段有原文可对"，判据 8 也顺手多一个样本
        units.append((f"{KNOWN_TEXT[:4]}|known", synth[0], synth[1]))
    if len(units) == 1:
        # 只有一段可用音频就把同一段切前后两半喂：判据 8 要的是"内容不同→文本不同"，
        # 两半就足够把它判红或判绿，不必为它再依赖一次出网合成。
        name, pcm, _secs = units[0]
        half = len(pcm) // 2 // BROWSER_FRAME * BROWSER_FRAME
        units = [(f"{name}|前半", pcm[:half], half / BYTES_PER_SECOND),
                 (f"{name}|后半", pcm[half:], (len(pcm) - half) / BYTES_PER_SECOND)]
    if not units:
        record(None, "2 有可放的音频（≥1.2s 的 16bit wav）",
               f"{args.samples} 里一段能放的都没有，合成也没成｜这个目录本该挂上游仓库里的 "
               f'fay/samples/（"git submodule update --init fay" 就有了）（{ENV_SKIP_MARK}）')
        for name, why2 in (("3 说话过程中就收到过 partial（不是收工才一次性回）", "没有可放的音频"),
                           ("4 每段恰好一个 is_final，且没有过早的 final", "没有可放的音频"),
                           ("5 final 文本里真的有中文", "没有可放的音频"),
                           ("6 累计文本单调（后一帧以前一帧为前缀）", "没有可放的音频"),
                           ("7 已知原文那句话认出了关键词", "合成不可用"),
                           ("8 不同音频的 final 互不相同（不是固定回一句）", "没有可放的音频"),
                           ("9 字数与时长同量级（0.5~15 字/秒）", "没有可放的音频")):
            record(None, name, why2)
        return finish()
    record(True, "2 有可放的音频（≥1.2s 的 16bit wav）",
           "、".join(f"{label}:{secs:.2f}s" for label, _, secs in units))
    run(args.host, args.port, args.path, units, args.timeout)
    return finish()


def finish() -> int:
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = sum(1 for ok, _, _ in RESULTS if ok is False)
    skipped = [r for r in RESULTS if r[0] is None]
    tail = f"，{len(skipped)} 项 SKIP" if skipped else ""
    print(f"\n[asr-probe] {passed}/{len(RESULTS)} 通过{tail}"
          + (f"，{failed} 项失败" if failed else ""))
    if DEGRADED_ASR_HOST:
        print(f"  环境证据：{DEGRADED_ASR_HOST}")
    for _ok, name, detail in skipped:
        print(f"  – {name}  {detail}")
    for _ok, name, detail in RESULTS:
        if _ok is False:
            print(f"  ✗ {name}  {detail}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
