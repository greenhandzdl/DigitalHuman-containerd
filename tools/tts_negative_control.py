"""「外部 TTS 出口不通」与「容器 audio 推送坏了」两件事的受控变异实验台。

只在诊断口径本身被改动时跑，不在 ./run.sh test 的八组里 —— 它要靠一行 --add-host
才能造出「出口不通」那一臂，正常轮里造不出来。README「『外部 TTS 出口不通』和
『容器 audio 推送坏了』是两件事」一节里的两臂表格就是它跑出来的。

两臂唯一的差别是宿主机给的 DNS：
    docker run --rm --network digitalhuman_default \\
      --add-host speech.platform.bing.com:127.0.0.1 \\        # 只有"出口不通"臂加这行
      -v "$PWD/probes:/probe:ro" \\
      -v "$PWD/overlay/fay/system.conf:/app/system.conf:ro" \\
      -v "$PWD/overlay/fay-lite/config.json:/app/config.json:ro" \\
      -v "$PWD/tools:/tools:ro" -w /app dh-fay:local python /tools/tts_negative_control.py

它不打 LLM、不占显存，两臂加起来十几秒。挂载全是 :ro，不动任何在跑的实例。
"""

import os
import sys
import time

sys.path.insert(0, os.environ.get("PROBE_DIR", "/probe"))
import fay_probe as fp  # noqa: E402

# 下游那条判据只看帧里有没有带 HttpValue 的 audio；这里手写一份"一帧 audio 都没有"的
# 会话，好让两臂拿到完全相同的输入 —— 差异必须只来自 check_tts() 那次实测。
NO_AUDIO_SESSION = [{"Topic": "human", "Data": {"Key": "log", "Value": "探针造的无音频会话"}}]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://fay-lite:5000"
    t0 = time.monotonic()
    fp.check_tts()
    tts = fp.RESULTS[-1]
    print(f"check_tts 用时 {time.monotonic() - t0:.1f}s → {_lbl(tts[0])}｜TTS_OK={fp.TTS_OK}"
          f"｜{tts[2]}", flush=True)
    fp.check_audio_url(NO_AUDIO_SESSION, base)
    audio = fp.RESULTS[-1]
    print(f"下游 audio 帧判据 → {_lbl(audio[0])}｜{audio[1]}｜{audio[2]}", flush=True)
    # 收尾那行顺带把 skip_tail 的分类口径也演示一遍：出口不通那一臂会走"与 LLM 无关"分支。
    print("skip_tail:" + fp.skip_tail([r for r in fp.RESULTS if r[0] is None]), flush=True)
    if audio[0] is tts[0] is not None:
        print("两臂判据没分开：出口通时不该 SKIP、出口不通时不该 FAIL", flush=True)
        return 1
    return 0


def _lbl(ok: bool | None) -> str:
    return {True: "PASS", False: "FAIL", None: "SKIP"}[ok]


if __name__ == "__main__":
    sys.exit(main())
