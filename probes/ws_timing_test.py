#!/usr/bin/env python3
"""探针自己的收工时机测试：假 Fay 的 WS 服务端，不打 LLM、不看显存，几秒跑完。

为什么要有这一份：`check_ws()` 决定「这个会话可以收工了」的那段时间逻辑，本身就是
一条会出错的判据。run #10 里 `Data.Key=audio` 被报成 FAIL，而同一份代码在 run #9
里是 PASS —— 差异只可能来自时间（audio 帧要等整句 TTS 合成完才推，6s 静默窗口盖不住）。
一条会因为被测对象的**节奏**而变红的判据，如果不单独钉住，下次改窗口长度就只能靠
再跑 20 分钟全套去猜。所以这里把三种时机各量一次，全部用假服务端按脚本出帧：

  A) 迟到的 audio（text 之后 18s 才来）—— 必须收到。同时把窗口调回旧的 6s 再跑一遍，
     必须**收不到**：这条不是装饰，它证明 A 的通过确实是窗口长度带来的，而不是
     假服务端碰巧配合。
  B) audio 永远不来 —— 不能无限等。text 之后再等一个 grace 窗口就必须收工。
  C) 没声明 Output 的会话 —— 不被 audio 宽限牵连，仍按 6s 静默收工（防止为了修 A
     把整条 WS 判据都拖成慢判据）。
  D) 回答本身来得晚（text 在 12s）—— 收工判定不能被「一静就收」提前触发。
  E) 服务端在 text 之后就关连接 —— 已收到的帧仍然是证据，不能因为连接没了就报
     「一帧都没收到」。
  F) 先推一帧空 Value 的 text 终止帧、20s 后才给真句子 —— 空帧不能算「已经开始
     回答」（run #20 的 fay 组就是被这一帧骗成 41s 假红，见 check_ws 的 _has_substance）。

用法：python3 ws_timing_test.py        退出码 = 失败断言数。
容器内跑（镜像里是上游钉的 websockets 10.4）与宿主机跑（16.x）都要过，
所以 serve 的导入做了两版兼容，handler 签名写成 (ws, path=None)。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import websockets  # noqa: E402
import fay_probe as fp  # noqa: E402

try:  # websockets >= 14
    from websockets.asyncio.server import serve
except ImportError:  # 上游钉住的 10.4（legacy 实现）
    from websockets.legacy.server import serve  # type: ignore

WS_PORT = int(os.environ.get("WS_TIMING_PORT", "18993"))
HTTP_PORT = int(os.environ.get("WS_TIMING_HTTP_PORT", "18994"))


class _Http(BaseHTTPRequestHandler):
    """check_ws 会先 POST 一次 /api/send 触发这轮回答，这里只需照单接收。"""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"key":"ok"}')

    def log_message(self, *a):
        pass


def _frame(key: str, value: str | None = None) -> str:
    return json.dumps(
        {"Topic": "human",
         "Data": {"Key": key, "Value": f"<{key}>" if value is None else value,
                  "HttpValue": "",
                  "IsFirst": 1 if key == "text" else 0,
                  "IsEnd": 1 if key == "text" and value == "" else 0}},
        ensure_ascii=False)


async def _scripted(ws, script, path=None):
    await ws.recv()  # 注册帧 {Username, Output}
    t0 = time.monotonic()
    try:
        for when, key, *rest in script:
            await asyncio.sleep(max(0.0, t0 + when - time.monotonic()))
            await ws.send(_frame(key, *rest))
        async for _ in ws:  # 保持连接：收工时机由客户端决定
            pass
    except websockets.exceptions.ConnectionClosed:
        # 「退回旧窗口」那条就是靠客户端提前收工来复现漏帧的，服务端这里的写失败
        # 是预期路径，别让 websockets 打一段 traceback 混进结论里
        pass


async def _closing(ws, path=None):
    await ws.recv()
    for key in ("log", "question", "text"):
        await ws.send(_frame(key))
    await asyncio.sleep(0.3)
    await ws.close()  # 模拟服务端答完就断


def _keys(frames: list[dict]) -> list[str]:
    return sorted({str((f.get("Data") or {}).get("Key")) for f in frames})


async def _run(name: str, script, *, output: bool, timeout: float, grace: float):
    fp.AUDIO_GRACE_SECONDS = grace
    fp.record = lambda *a, **k: None  # 这一份只验时机，不要把探针的判定行混进输出
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), _Http)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with serve(lambda w: _scripted(w, script), "127.0.0.1", WS_PORT):
            started = time.monotonic()
            frames = await fp.check_ws("127.0.0.1", WS_PORT, "t_" + name,
                                       f"http://127.0.0.1:{HTTP_PORT}", timeout, output)
            return time.monotonic() - started, frames
    finally:
        srv.shutdown()


async def main() -> int:
    late = [(0.2, "log"), (0.4, "question"), (1.0, "text"), (18.0, "audio")]
    checks = []

    e, fr = await _run("late", late, output=True, timeout=30, grace=45)
    checks.append(("text 之后 18s 才到的 audio 帧能收到", "audio" in _keys(fr),
                   f"{e:.1f}s keys={_keys(fr)}"))
    e, fr = await _run("late6", late, output=True, timeout=30, grace=6)
    checks.append(("把窗口退回旧的 6s 就收不到（证明上一条是窗口长度起的作用）",
                   "audio" not in _keys(fr), f"{e:.1f}s keys={_keys(fr)}"))

    e, fr = await _run("noaudio", [(0.2, "log"), (1.0, "text")],
                       output=True, timeout=8, grace=20)
    checks.append(("audio 不来时按 grace 止损，不死等", 19 <= e <= 24, f"{e:.1f}s keys={_keys(fr)}"))

    e, fr = await _run("noflag", [(0.2, "log"), (1.0, "text")],
                       output=False, timeout=40, grace=45)
    checks.append(("Output=false 的会话不被 audio 宽限拖慢", 6 <= e <= 12, f"{e:.1f}s"))

    e, fr = await _run("slowtext", [(0.2, "log"), (12.0, "text")],
                       output=False, timeout=40, grace=45)
    checks.append(("text 来得晚也不会被「一静就收」漏掉", "text" in _keys(fr), f"{e:.1f}s"))

    # F) 终止帧先行：fork 在 :10002 上会先推一帧 Value 为空、IsFirst=IsEnd=1 的 text
    #    （上游不会），真正的句子 20s 之后才到。收工判定如果只看「帧里出现过 text 这个
    #    字符串」，就会被这帧空内容骗到、6s 静默后收工 —— run #20 的 fay 组正是这样
    #    在 21:27:44 关掉了会话，而那一发的回答 21:28:25 才落地。
    e, fr = await _run("emptyfirst",
                       [(0.2, "log"), (0.4, "question"), (1.0, "text", ""), (20.0, "text", "真回复")],
                       output=False, timeout=40, grace=45)
    got = [str((f.get("Data") or {}).get("Value")) for f in fr
           if (f.get("Data") or {}).get("Key") == "text"]
    checks.append(("空 text 终止帧不能算「已经开始回答」",
                   got == ["", "真回复"] and e >= 20,
                   f"{e:.1f}s text帧={got}"))

    # E) 服务端答完就断：帧要留下，且不能等到 deadline
    fp.AUDIO_GRACE_SECONDS = 45
    fp.record = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), _Http)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with serve(_closing, "127.0.0.1", WS_PORT):
            started = time.monotonic()
            fr = await fp.check_ws("127.0.0.1", WS_PORT, "t_closing",
                                   f"http://127.0.0.1:{HTTP_PORT}", 30, False)
            e = time.monotonic() - started
    finally:
        srv.shutdown()
    checks.append(("服务端断开后仍保留已收到的帧", len(fr) == 3 and e < 12, f"{e:.1f}s {len(fr)} 帧"))

    # G) 收尾那行的统计口径：SKIP 分两类 —— 「本机 LLM 没给出证据」而降级的，和与本轮
    #    LLM 无关的（无头的 yueshen/window capture、本机没起的 FunASR、外部 TTS 出口不通）。
    #    run #21 三个实例显存驻留都是 100%、问答全绿，那三条 SKIP 全属后一类，旧文案却
    #    一律写成「因本机 LLM 证据降级」，等于替环境揽下没发生过的嫌疑。
    boundary = [(None, "MCP 现场连接离线服务器 yueshen rag", "连接失败｜yueshen 属未覆盖能力"),
                (None, "MCP 现场连接离线服务器 window capture", "连接失败｜window capture 属未覆盖能力"),
                (None, "远程音频的 ASR 认出了文字", "本机没有起 FunASR 服务")]
    t_only_boundary = fp.skip_tail(boundary)
    checks.append(("三条 SKIP 都与 LLM 无关时，不许提「因本机 LLM 证据降级」",
                   "LLM 证据降级" not in t_only_boundary and "其余 3 项" in t_only_boundary,
                   t_only_boundary))
    # 降级那条不手写，用 latency_verdict 现造：这样「谁写标记」这件事也被测住了 ——
    # 以后改动 latency_verdict 的文案一旦漏掉标记，收尾统计就会悄悄少算。
    made: list[tuple[bool | None, str, str]] = []
    fp.record = lambda ok, name, detail="": made.append((ok, name, detail))
    old_degraded, fp.DEGRADED_LLM_HOST = fp.DEGRADED_LLM_HOST, "测试用：直连下限 99.9s、驻留 6%"
    fp.latency_verdict(False, "G 判据", "0 字")
    fp.DEGRADED_LLM_HOST = old_degraded
    fp.record = lambda *a, **k: None
    produced = made[0]
    t_mixed = fp.skip_tail([produced] + boundary)
    checks.append(("latency_verdict 降级的记录会被单独数进「LLM 证据降级」，其余归另一类",
                   fp.ENV_SKIP_MARK in produced[2]
                   and t_mixed == "，1 项因本机 LLM 证据降级为 SKIP，其余 3 项 SKIP（成因逐条列在下面，与本轮 LLM 证据无关）",
                   f"详情={produced[2]}｜尾缀={t_mixed}"))
    checks.append(("一条都没有时尾缀为空（不会留个孤零零的逗号）", fp.skip_tail([]) == "", repr(fp.skip_tail([]))))

    rc = 0
    for name, ok, detail in checks:
        print(("PASS  " if ok else "FAIL  ") + name + f"  —— {detail}")
        rc += 0 if ok else 1
    print(f"[ws-timing] {len(checks) - rc}/{len(checks)} 通过")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
