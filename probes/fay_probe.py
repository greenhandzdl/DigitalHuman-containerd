#!/usr/bin/env python3
"""Fay 侧功能探针（新增测试件，不改任何上游仓库）。

用 dh-fay / dh-origin-fay 镜像各起一个一次性容器执行（websockets、配置、
依赖都是现成的），而探针要验的正是 Fay 的对外契约：

  0. 基线   /v1/chat/completions(model=fay) —— Fay 自带的 OpenAI 兼容层，
                同时量一次本地 LLM 的真实耗时，用它给后面每次问答定预算：
                整机 llama-server 是被抢的，同一句话空闲 4.8s、压满 >90s，
                写死超时会把「机器忙」误判成「Fay 坏了」
  1. HTTP   /api/send + /api/get-msg   —— 文字问答（adapter 依赖的口子）
  2. WS     10002 human 通道           —— 注册 {Username, Output}，
                                          服务端推 {Topic:'human', Data:{Key,Value}}
  3. WS     10003 web  通道            —— 面板协议，扁平的 panelMsg/panelReply，
                                          与 10002 不同形，不能拿同一套断言去量
  4. WS 音频面 —— 同一条 10002 分别用 Output=false / Output=true 各连一次。
                两个方向不对称：没声明 Output 却收到 audio 是真故障（FAIL），
                声明了却没收到多半是这轮压根没回复（audio 帧的下游是回复的合成），
                所以后者按环境证据降级。audio 帧只跟客户端的 Output 走
                （get_client_output），与容器侧 playSound / 声卡无关；再按 audio
                帧的 HttpValue 做一次真实 HTTP 取文件，验客户端拿音频的那条 URL 确实能用。
                收工时机是这里最容易误判的地方：收到 text 之后再等一个静默窗口，窗口
                按「这个会话还欠不欠 audio」分两档 —— 欠着给 AUDIO_GRACE_SECONDS（audio
                实测落在 text 之后而不是同时，按 6s 收会把它漏成 FAIL），不欠就 6s。
                服务端主动关连接不算故障，已收到的帧仍然是证据。
  5. TTS    tts/ms_tts_sdk.Speech      —— edge_tts 出网合成 mp3 再转 wav，
                                          to_sample 返回的是**文件路径**
  6. MCP    :5010 管理口 + :8765/sse   —— 列工具清单、真调 ping→pong 与知识库的
                                          kb_list_sources；不打 LLM，所以这条永不降级，
                                          是「MCP 通不通」的确定性凭据（依赖 mcp SDK 1.x）。
                                          另加一条 prestart：注册一个「每轮说话之前无条件
                                          执行」的工具，再去 :10003 看这一轮的回答流里有没有
                                          它的输出 —— 这是第一次能证明「一句话真的驱动了工具」
                                          而不是「我们手动替它调了一次」
                                          yueshen rag 也在这里被连一次、列一次工具清单；
                                          它「语料真灌进去、真查得回来」那一步不在这里，
                                          见 probes/yueshen_probe.py（要抢宿主 ollama 的
                                          嵌入模型，不能插在问答轮次中间）。
  7. 控制台 —— :5000 根页面 + 它引用的每个 /static/* 逐个取，全 200 且非空才算过。
                这条只可能由容器化本身弄坏（少拷一层、.dockerignore 排掉静态目录），
                而问答链路完全感知不到，所以值得单独盯。
  8. 向量真伪 —— 直接调 simulation_engine.gpt_structure.get_text_embedding（仿生记忆用的
                就是它），再拿同一文本把上游那条「API 失败 → 本地伪向量」的兜底路径原样
                算一遍逐维比对。上游这个兜底是静默的（只 print 一行），所以「记忆在工作」
                和「记忆存的是假坐标」在别的判据里长得一模一样，只能单独量。
  9. 语音输入 —— 探针自己当远程麦克风：连 TCP :10001、注册用户名、按实时速率推 4 秒
                满幅方波 + 静音，看服务端的 VAD 认不认（:10002 上有没有「聆听中」的 log 帧）。
                这条路无声卡也能走（core/recorder.py:251 对 is_remote() 免检 record.enabled），
                所以「容器没声卡所以语音输入整条不会触发」这句老说明只对麦克风成立。
                识别本身另记一条：本栈 ASR 是 funasr 客户端，后端服务不在线就 SKIP。
 10. 决策面谈 —— :5001（genagents）不是常驻端口，是 `POST :5000/api/start-genagents`
                按需拉起的子服务，所以「5001 没监听」本身不是缺陷。这里判起落两向：
                启动路由 200 + :5001 真 bind → 页面真渲染出传进去的那句指令 →
                `POST :5001/api/shutdown` 之后端口在时限内关回不监听。
                注意它回报的 url 是硬编码的 127.0.0.1（flask_server.py:1803），探针在
                另一个容器里，照那个地址连只会连到自己，所以真正拨号用容器名。

  执行顺序按「证据强度」排：先 HTTP 就绪与控制台（7），再绕过 Fay 直连 LLM 量环境
  证据，然后是确定性的 MCP（6，不打 LLM）、语音输入（9，不打 LLM 也不等回复）与
  决策面谈（10，只验镜像能不能在容器里拉起子服务），
  最后才轮到依赖显存里那个模型的问答 / WS /
  音频判据，向量真伪（8）排在最末（它和问答抢同一个 ollama 出口）。
  这样机器被别的容器占满时，能判的结论已经全部落袋，不会一起变红。

用法：python probe.py [--base http://fay:5000] [--timeout 90] [--max-timeout 420]
退出码 = 失败的检查项数（0 表示全绿）。

三态而不是两态：PASS / FAIL / SKIP。SKIP 只用于「这台机器的显存不够，这条判不了」，
且必须同时给出客观证据（ollama /api/ps 的权重驻留显存比例 + 直连 LLM 的下限耗时）。
判得过就必须判，不能用 SKIP 掩盖故障；问答链路的真实通不通由轻模型实例兜住
（compose 里的 fay-lite，见 README「真实瓶颈是显存」）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RESULTS: list[tuple[bool | None, str, str]] = []  # (ok, name, detail)，ok=None → SKIP
_LABELS = {True: "PASS", False: "FAIL", None: "SKIP"}

# 本机 LLM 慢到判不了问答时置真，由 check_llm_host() 用实测数据决定。
DEGRADED_LLM_HOST: str | None = None
# 凡「这条 SKIP 的成因是本机的 LLM 环境」的判据，详情里都带上这个串。收尾统计靠它把
# 「环境不够、判不了」和「本来就覆盖不到的边界」分开数 —— 不能让读者混着看。
ENV_SKIP_MARK = "按环境降级"
# 绕过 Fay 直连 LLM 出口量到的下限耗时（秒），0 表示没量到。
DIRECT_LLM_SECONDS: float | None = None


def record(ok: bool | None, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"{_LABELS[ok]}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def latency_verdict(ok: bool, name: str, detail: str) -> None:
    """耗时类判据专用：真不过就是不过，除非有证据说明是这台机器的显存不够。"""
    if ok:
        record(True, name, detail)
    elif DEGRADED_LLM_HOST:
        record(None, name, f"{detail}｜{ENV_SKIP_MARK}为 SKIP：{DEGRADED_LLM_HOST}")
    else:
        record(False, name, detail)


def skip_tail(skipped: list[tuple[bool | None, str, str]]) -> str:
    """把收尾那行「N 项因本机 LLM 证据降级」写成分类计数，而不是一律推给 LLM。

    两类 SKIP 混着数会撒谎：run #21 三个实例显存驻留都是 100%、问答全绿，剩下三条
    SKIP 是无头的 yueshen/window capture 和本机没起的 FunASR，跟本轮 LLM 证据一点关系
    没有，旧那句却把它们全记成「因本机 LLM 证据降级」。区分凭据是详情里的
    ENV_SKIP_MARK —— 它由判据自己写上，不是从人话里猜的。
    """
    degraded = [r for r in skipped if ENV_SKIP_MARK in r[2]]
    other = [r for r in skipped if ENV_SKIP_MARK not in r[2]]
    tail = ""
    if degraded:
        tail += f"，{len(degraded)} 项因本机 LLM 证据降级为 SKIP"
    if other:
        tail += f"，其余 {len(other)} 项 SKIP（成因逐条列在下面，与本轮 LLM 证据无关）"
    return tail


# 管线自己承认失败的兜底语（fork: llm/nlp_cognitive_stream.py:2258 / 2871 / 2945 / 3066）。
# 出现这些串 == LLM 调用超时或结果解析失败，不能算「问答链路通了」——
# run #2 里 /api/send 那条 243.3s 拿到的正是第 1 条，当时被当成 PASS 记了下来。
PIPELINE_FALLBACKS = (
    "抱歉，我的大脑暂时开了小差",
    "抱歉，处理结果时出了点问题",
    "抱歉，我现在太忙了",
)

# 这两台 MCP 服务器在 Linux 容器里注定连不上，原因记在 README「未覆盖能力」：
#   window capture —— 只服务 Windows 桌面截图
#   logseq         —— 配置里的 graph 目录是开发者本机的 D:\iCloudDrive\...
# 只有名单内的连不上才允许 SKIP；名单外连不上 = 真故障，报 FAIL。
# 反过来，它们哪天真连上了就记 PASS，不把话说死。
#
# yueshen 曾在名单里，现在不在了：它有了自己的镜像与服务（compose 里的 yueshen-rag +
# patches/yueshen_rag/0001 那条 SSE 分支），连不上就是真故障。
# 它「灌进去没有、查得回来没有」不由这里判，见 probes/yueshen_probe.py ——
# 那一步要打宿主 ollama 的嵌入模型，不能插在问答轮次中间抢显存。
HEADLESS_UNAVAILABLE = ("window capture", "window_capture", "logseq")

# 等 Fay 自己的「开机自连 MCP」跑完的上限。取 90s 的依据：实测自连在 :5010 开始监听
# 之后 1.6s 内完成（13:48:19.6 bind → 13:48:21.2 两台全 online），而整台机器被 9b
# 压满时这段会被 embedding 重试拖长（同一次开机里 :8765 从 5s 变 61s）。
# 90s 盖住实测最坏值，等不到就是真故障，不用更长时限把问题糊过去。
AUTOSTART_WAIT_SECONDS = 90.0

# :10002 吐空之后、重问之前的等待。run #25 fork 组两发全空，容器日志把成因钉死了：
# 前一轮 :10003 那句自我介绍被 fork 判成「闲聊判断器 finish 过长(106字)，追加核实」，
# 于是起了一条后台工具链（01:45:26 启动 → 01:47:04 才把回复写回原会话，98s 独占 9b），
# 第 1 发（01:45:41）与第 2 发（01:47:27）都落在它的阴影里；同一轮 01:48:13 的
# Output=true 那发拿到 53 字 —— 契约本身是通的，缺的是显存。所以重问要等的是
# 「上一轮遗留的后台链跑完」，而不是把同一个请求再发一遍。120s 取实测 98s 加余量，
# 只有真吐空才付这段时间。
EMPTY_REPLY_DRAIN_SECONDS = 120.0

# 声明了 Output=true 之后，为等 audio 帧额外放宽的静默上限。依据是 run #9/#10 的
# 对照：同一份探针代码，#9 里这条 PASS（真取回 357780 字节 wav），#10 里 text 帧已经
# 收到、之后 6s 静默内一帧没来，于是判成 FAIL —— 差异只可能来自时间。audio 的下游是
# 「整句合成完才推」，实测它落在 text 之后而不是同时，6s 盖不住。给 45s：够盖最坏的
# 合成节奏，且这条判据真坏时一组只多花 45s，不用更长时限把故障糊过去。
# 窗口本身由 probes/ws_timing_test.py 用假 Fay 钉住（含"退回 6s 必须收不到"那条）。
AUDIO_GRACE_SECONDS = 45.0


def reply_verdict(text: str) -> tuple[bool, str]:
    """(有没有真回复, 说明)。兜底语按「没回复」处理，并把失败原因写进断言里。"""
    body = (text or "").strip()
    if not body:
        return False, "空回复"
    if next((s for s in PIPELINE_FALLBACKS if s in body), None):
        return False, f"这是 Fay 管线内部的兜底语，不是回复：{body[:40]}"
    return True, body[:40]


def openai_post(chat_url: str, payload: dict, timeout: float) -> tuple[float, str]:
    """POST 一个 OpenAI 格式的 /chat/completions，返回 (耗时, content 文本)。"""
    started = time.monotonic()
    req = urllib.request.Request(
        chat_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read().decode("utf-8", errors="replace")
    elapsed = time.monotonic() - started
    try:
        text = json.loads(blob)["choices"][0]["message"]["content"].strip()
    except Exception:
        text = blob[:80]
    return elapsed, text


def check_llm_host(base: str, timeout: float) -> None:
    """量一次「绕过 Fay 全部业务逻辑、直连 system.conf 里配的 LLM 出口」的下限耗时。

    作用是把环境问题从功能问题里摘出去：Fay 的一次问答 = 直连下限 × 规划器链路倍数。
    直连本身就要几十上百秒时，Fay 侧的超时兜底是结果，不是容器集成出来的故障，
    于是后面几条耗时类判据降级为 SKIP（见 latency_verdict）。
    证据取 ollama /api/ps 的 size_vram/size：权重有多少真进了显存，比例低就是
    其余部分在 CPU 上跑（实测本机 qwen3.5:9b 只有 387MB/6.1GB = 6% 进显存）。
    """
    global DEGRADED_LLM_HOST
    chat_url = origin = None
    try:
        sys.path.insert(0, os.getcwd())
        from utils import config_util as cfg

        cfg.load_config()
        # 与被测容器同一套 containerd 补丁的开关：模型名可以被环境变量换掉
        # （fay-lite 就把 9b 换成 qwen2.5:1.5b），探针必须量同一个模型，否则
        # 拿着 system.conf 里的 9b 去判一个跑 1.5b 的实例，证据是错的。
        model = os.getenv("FAY_GPT_MODEL_ENGINE") or cfg.gpt_model_engine
        base_v1 = (cfg.gpt_base_url or "").rstrip("/")
        origin = base_v1[: -len("/v1")] if base_v1.endswith("/v1") else base_v1
        chat_url = f"{base_v1}/chat/completions"
    except Exception as exc:
        record(False, "本地 LLM 主机就绪（直连，不经 Fay）", f"读取 system.conf 失败: {exc!r}")
        return
    name = "本地 LLM 主机就绪（直连，不经 Fay）"
    payload = {"model": model, "stream": False,
               "messages": [{"role": "user", "content": "只回一个字：好"}]}

    def residency():
        """(驻留比例|None, 说明, 错误|None)。权重有多少真进了显存，是问答耗时的唯一解释变量。"""
        try:
            with urllib.request.urlopen(origin + "/api/ps", timeout=10) as resp:
                loaded = json.loads(resp.read().decode())["models"]
        except Exception as exc:
            return None, "", f"{origin}/api/ps {exc!r}"
        row = next((m for m in loaded if str(m.get("name", "")).startswith(model or "")), None)
        if not row or not row.get("size"):
            return None, "模型未常驻(首次请求要先换入)", None
        frac = float(row.get("size_vram") or 0) / float(row["size"])
        return frac, (f"{model} 权重驻留显存 {frac:.0%}"
                      f"（{row.get('size_vram', 0) / 1e6:.0f}/{row['size'] / 1e6:.0f}MB）"), None

    # 最多量两发。第二发只为一种情况存在：第一发很慢或没成，**并且**目标模型压根没在
    # 显存里 —— 那可能只是首次换入的一次性成本（实测 1.5b 冷换入 72.9s，换入后同一发
    # 0.0s）。只量一发就判降级，会把 fay-lite 这组唯一的正面问答证据也一起 SKIP 掉
    # （run #6 的冷启动就是这个场景）。反过来，已经量到「驻留不到一半」就是持续算力不足，
    # 再等第二发只是把同一件事等两遍。
    two = False
    for attempt in (1, 2):
        if attempt == 2:
            two = True
        try:
            elapsed, text = openai_post(chat_url, payload,
                                        timeout if attempt == 1 else min(timeout, 120.0))
            ok, why = reply_verdict(text)
            err, err_kind = None, None
        except Exception as exc:
            elapsed, ok, why = None, False, f"直连请求失败 {exc!r}"
            err = repr(exc)
            err_kind = ("timeout" if isinstance(exc, TimeoutError)
                        or "timed out" in str(exc).lower() else "error")
        gpu_frac, frac_txt, rerr = residency()
        if rerr:
            record(False, name, rerr)
            return
        if attempt == 1:
            warm = ok and elapsed is not None and elapsed <= 30 and (gpu_frac is None or gpu_frac >= 0.5)
            timed_out = err_kind == "timeout"
            if warm or (timed_out and gpu_frac is not None and gpu_frac < 0.5):
                break
            continue
        break

    if not ok:
        if err_kind == "timeout" and (gpu_frac is None or gpu_frac < 0.5):
            # 直连超时是**环境证据**，不是容器故障：run #5 的 origin-fay 组里，同一条
            # 日志的 frac_txt 已经写了「驻留显存 6%」，判据却报 FAIL —— 自相矛盾。
            DEGRADED_LLM_HOST = f"本机 LLM 直连超时（{frac_txt}）"
            record(None, name, f"{frac_txt}；直连 {timeout:.0f}s 未返回 {err}"
                               + ("（又补量一发仍未返回）" if two else "")
                               + f"｜{ENV_SKIP_MARK}，后续问答类判据同样 SKIP")
            # 故意不把 DIRECT_LLM_SECONDS 记成 timeout：未知耗时下 first 退回 --timeout，
            # 让后面每条问答在 90s 处快速失败并 SKIP，而不是干等 420s。
        else:
            record(False, name, f"{frac_txt}；{why}" + (f"（{err}）" if err else ""))
        return
    # 阈值来源是本机的实测分布，不是猜的：1.5b 全权重进显存时暖态 0.2s，
    # 9b 只有 6% 进显存时 165~240s。取 30s 而不是 60s：显存被挤走的 9b 直连一发
    # 只生成 8 个 token 也实测到 39.1s（run #3），60s 阈值会把这种「CPU 在推」
    # 的状态误判成健康，然后后面每条问答都跑到 600s 上限才失败。
    detail = (f"{elapsed:.1f}s {frac_txt}；回复={why}"
              + ("（第一发是换入成本，不计入基线）" if two else ""))
    if elapsed > 30 or (gpu_frac is not None and gpu_frac < 0.5):
        DEGRADED_LLM_HOST = f"本机 LLM 直连下限 {elapsed:.1f}s、{frac_txt}"
        record(None, name, detail + f"｜{ENV_SKIP_MARK}：显存不足，问答耗时类判据将降级为 SKIP")
    else:
        global DIRECT_LLM_SECONDS
        DIRECT_LLM_SECONDS = elapsed
        record(True, name, detail)


def check_mcp_tools(base: str, timeout: float = 20.0) -> None:
    """MCP 工具链的真凭据：不打 LLM，直接走 mcp_service 的管理口（5010）。

    这条是确定性的，不吃显存、不占问答预算，所以永远不该降级成 SKIP。
    它把「MCP 到底通不通」和「模型会不会用工具」分开：前者由这里证，
    后者要等一个能真的调工具的模型 —— 本机显存不够时那条判不了（见 README）。

    依赖 mcp SDK 1.x：镜像里钉 mcp>=1.2,<2（overlay/*/requirements-docker.txt），
    因为 2.x 把 mcp.server.Server 换成了高层 MCPServer，仓库里 6 个按低层
    @server.list_tools() 写的服务器全部 AttributeError。

    调 ping 时传 is_prestart=true：只跳过「工具被用户在 UI 里禁用」这层状态检查，
    工具本身该正常返回；测试不该依赖别人的 UI 开关状态。
    """
    host = urllib.parse.urlparse(base).hostname or "127.0.0.1"
    svc = f"http://{host}:5010"

    def get(path: str):
        with urllib.request.urlopen(svc + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        raw = get("/api/mcp/servers")
    except Exception as exc:
        # 5010 只在容器网络里监听（compose 没往宿主发布），从宿主跑探针打不到
        if host in ("127.0.0.1", "localhost", "::1"):
            record(None, "MCP 管理面 /api/mcp/servers (:5010)", f"{exc!r}｜5010 未发布到宿主，需在容器网络内跑探针")
        else:
            record(False, "MCP 管理面 /api/mcp/servers (:5010)", repr(exc))
        return
    servers = raw.get("servers") if isinstance(raw, dict) else raw
    if not isinstance(servers, list) or not servers:
        record(False, "MCP 管理面 /api/mcp/servers (:5010)", f"返回里没有服务器清单: {str(raw)[:80]}")
        return

    # 先等 Fay 自己的「开机自连」跑完，再取快照。这不是装饰：:5010 开始监听比自连完成
    # 早一两秒，run #8 的 lite 组就是这么被量错的 —— 探针 20.9s 打到管理面，
    # 读到「在线 无」判 FAIL，而自连在 21.2s 才把 tools/课程知识库 标成 online；
    # 紧接着下面那段现场连接又把这两台重连一遍，同一台服务器起两个 stdio 子进程。
    # 「autostart=true 的服务器最终都在线」本身就是这条判据的内容，等它 = 验它。
    wanted = [s for s in servers if s.get("autostart")]
    start = time.monotonic()
    pending = {s["id"] for s in wanted}
    while pending and time.monotonic() - start < AUTOSTART_WAIT_SECONDS:
        time.sleep(2.0)
        try:
            raw = get("/api/mcp/servers")
        except Exception:
            continue
        servers = raw.get("servers") if isinstance(raw, dict) else raw
        online_ids = {s["id"] for s in servers if s.get("status") == "online"}
        pending -= online_ids
    waited = time.monotonic() - start
    online = [s for s in servers if s.get("status") == "online"]
    online_ids = {s["id"] for s in online}
    still_off = [f"{s['id']}:{s.get('name')}" for s in wanted if s["id"] not in online_ids]
    detail = (f"{len(servers)} 台配置，在线 {[(s['id'], s.get('name')) for s in online] or '无'}"
              f"｜autostart {len(wanted)} 台"
              + (f"全部自连完成（等 {waited:.0f}s）" if not still_off
                 else f"，仍未在线 {still_off}"))
    if wanted and still_off:
        record(False, "MCP 管理面 /api/mcp/servers (:5010)",
               detail + f"｜开机自连等满 {AUTOSTART_WAIT_SECONDS:.0f}s")
    elif online:
        record(True, "MCP 管理面 /api/mcp/servers (:5010)", detail)
    else:
        # 配置里一台 autostart 都没有：在线清单为空不是故障，可达性交给下面的现场连接去证。
        record(None, "MCP 管理面 /api/mcp/servers (:5010)",
               detail + "｜没有任何 autostart 服务器，在线为空不算故障")

    def post(path: str) -> dict:
        req = urllib.request.Request(svc + path, data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            # connect 要现场 spawn 一个 python 子进程并跑完 MCP 握手，比读清单慢得多
            with urllib.request.urlopen(req, timeout=max(timeout, 60.0)) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:   # 连接失败返回 500，正文里带原因
            try:
                return json.loads(exc.read().decode("utf-8", errors="replace"))
            except Exception:
                return {"success": False, "message": str(exc)}

    # autostart 是人在 UI 上点的开关，测试不该依赖它的状态：把离线的服务器现场连起来
    # 一起验，验完由探针断开，恢复原样。
    reachable = list(online)
    connected: list[int] = []
    for s in servers:
        if s.get("status") == "online":
            continue
        sid, name = s["id"], str(s.get("name"))
        try:
            out = post(f"/api/mcp/servers/{sid}/connect")
        except Exception as exc:
            record(False, f"MCP 现场连接离线服务器 {name}", repr(exc))
            continue
        if out.get("success"):
            connected.append(sid)
            reachable.append(out.get("server") or dict(s, status="online"))
            record(True, f"MCP 现场连接离线服务器 {name}",
                   f"连上并取到 {len(out.get('tools') or [])} 个工具，验完断开")
        else:
            msg = str(out.get("message") or "")[:100]
            known = next((k for k in HEADLESS_UNAVAILABLE if k in name.lower()), None)
            record(None if known else False, f"MCP 现场连接离线服务器 {name}",
                   msg + (f"｜{known} 属 README「未覆盖能力」记的边界" if known else ""))

    if not reachable:
        record(False, "MCP 管理面：有可用服务器", "在线的一台没有，现场连接也全部失败")
        return

    def call_tool(sid: int, method: str, params: dict):
        body = json.dumps({"method": method, "params": params, "is_prestart": True}).encode()
        req = urllib.request.Request(svc + f"/api/mcp/servers/{sid}/call", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = "".join(c.get("text", "") for c in ((out.get("result") or {}).get("content") or [])
                       if isinstance(c, dict))
        return bool(out.get("success")), text, out

    pinged, kbd, sched = None, None, None
    for s in reachable:
        sid = s["id"]
        try:
            names = [t.get("name") for t in (get(f"/api/mcp/servers/{sid}/tools") or {}).get("tools", [])]
        except Exception as exc:
            record(False, f"MCP 工具清单 server_id={sid} {s.get('name')}", repr(exc))
            continue
        record(bool(names), f"MCP 工具清单 server_id={sid} {s.get('name')}", f"{names}")
        if "ping" in names and pinged is None:
            pinged = sid
        # 「课程知识库」的 kb_list_sources 只读、不打 LLM，是验证知识库真能被调起来的
        # 最省事的一条（fork 的问答要靠它出课程内容，光看开机日志 count=9 不算调用过）
        if "kb_list_sources" in names and kbd is None:
            kbd = sid
        # 日程管理同理：康养场景里「到点提醒吃药/休息」就是靠它，get_schedules 只读
        if "get_schedules" in names and sched is None:
            sched = sid
    if pinged is None:
        # 连完还是没有任何一台暴露 ping —— 这条判据根本没被执行，不能算 PASS
        # （run #4 里它被印成 PASS 就是假绿，详情写着「没有暴露 ping」却标通过）。
        record(None, "MCP stdio 示例工具真调用 (ping→pong)",
               "在线与现场连接后可达的服务器里都没有暴露 ping，只验到工具清单")
    else:
        try:
            ok, text, out = call_tool(pinged, "ping", {})
        except Exception as exc:
            record(False, "MCP stdio 示例工具真调用 (ping→pong)", repr(exc))
        else:
            record(ok and "pong" in text, "MCP stdio 示例工具真调用 (ping→pong)",
                   f"text={text[:40]!r}" if ok else str(out)[:120])
    for sid, tool, label in ((kbd, "kb_list_sources", "知识库"), (sched, "get_schedules", "日程")):
        if sid is None:
            continue
        try:
            ok, text, out = call_tool(sid, tool, {})
        except Exception as exc:
            record(False, f"MCP {label}工具真调用 ({tool})", repr(exc))
            continue
        if tool == "get_schedules":
            # 返回值契约是 JSON 数组字符串；解析不了就是工具没真跑起来，不是「慢」
            try:
                rows = json.loads(text)
            except Exception:
                rows = None
            record(ok and isinstance(rows, list), f"MCP {label}工具真调用 ({tool})",
                   f"{len(rows)} 条日程，首条 {str(rows[:1])[:80]}"
                   if isinstance(rows, list) else (text or str(out))[:120])
        else:
            record(ok and len(text.strip()) > 2, f"MCP {label}工具真调用 ({tool})",
                   f"{len(text)} 字: {text[:60]!r}" if ok else str(out)[:120])

    # 断开只针对「探针自己连上的」那几台，autostart 起来的保持原状
    for sid in connected:
        try:
            post(f"/api/mcp/servers/{sid}/disconnect")
        except Exception as exc:
            print(f"  ! 断开 server_id={sid} 失败（一次性探针容器退出后由 Fay 的连接检查兜底）: {exc!r}",
                  flush=True)


def wait_port(host: str, port: int, wait: float) -> float:
    """等到 port 开始监听，返回等待秒数；超时返回 -1。

    这条等待是必需的而不是兜底装饰：:8765 不是 Fay 第一批起来的口。实测一次开机
    5000/5010/10002/10003 在第 6 秒就全监上了，:8765 却要到第 61 秒才 bind
    （MCP SSE 的 uvicorn 排在开机自检那串 embedding 重试之后）。run #7 里 lite 组
    的 `MCP SSE 握手` 报 ConnectionRefused 就是探针在 24 秒时打过去、端口还差 0.4
    秒才 bind —— 那是量早了，不是功能坏了。
    """
    start = time.monotonic()
    while time.monotonic() - start < wait:
        sock = socket.socket()
        sock.settimeout(2.0)
        try:
            if sock.connect_ex((host, port)) == 0:
                return time.monotonic() - start
        finally:
            sock.close()
        time.sleep(1.0)
    return -1.0


def check_console(base: str, timeout: float = 20.0) -> None:
    """Fay 自带的 Web 控制台：根页面 + 它自己引用的每一个 /static/* 都要 200。

    这条完全不碰 LLM，盯的是容器化本身：`web/` 少拷一层、`.dockerignore` 把静态目录
    排掉，问答链路照样全绿（后端和数字人前端都不读控制台 HTML），只有人在浏览器里
    才会发现管理台是白的 —— 所以必须有个测试件替人盯着。宿主上实测 22 个资源全 200。
    """
    try:
        with urllib.request.urlopen(base + "/", timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            code = resp.status
    except Exception as exc:
        record(False, "Fay Web 控制台 (:5000 根路径)", repr(exc))
        return
    if code != 200 or "<script" not in html:
        record(False, "Fay Web 控制台 (:5000 根路径)", f"HTTP {code}，{len(html)} 字节，不像 HTML")
        return
    refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"?]+)', html)))
    bad = []
    for path in refs:
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as resp:
                size = int(resp.headers.get("Content-Length") or 0)
                resp.read(1)
                if resp.status != 200:
                    bad.append(f"{path}={resp.status}")
                elif size == 0:
                    bad.append(f"{path}=空文件")
        except Exception as exc:
            bad.append(f"{path}={exc!r}")
    record(not bad, f"Fay Web 控制台静态资源 ({len(refs)} 个 /static/*)",
           "全部 200 且非空" if not bad else "异常: " + "; ".join(bad[:6]))


def check_mcp_sse(host: str, timeout: float = 15.0, wait: float = 120.0) -> None:
    """MCP SSE 端口 8765 握手：这条是 uvicorn<0.35 + websockets~=10.4 两道钉法的验收。

    mcp 带进来的 uvicorn 0.53 在 protocols/websockets/auto.py 直接
    import websockets.server.ServerProtocol（10.4 里没有），SSE 服务起不来；
    之前只能靠「日志里有一行启动中」判断，这里真连一次：
    GET /sse 应当以 `event: endpoint` + `data: /messages?...session_id=...` 开场。
    """
    waited = wait_port(host, 8765, wait)
    if waited < 0:
        record(False, "MCP SSE 握手 (:8765/sse)", f"等了 {wait:.0f}s :8765 仍未监听")
        return
    url = f"http://{host}:8765/sse"
    suffix = "" if waited < 5 else f"（等 :8765 bind 用了 {waited:.0f}s）"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            head = []
            deadline = time.monotonic() + timeout
            while len(head) < 2 and time.monotonic() < deadline:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    head.append(text)
    except Exception as exc:
        record(False, "MCP SSE 握手 (:8765/sse)", repr(exc) + suffix)
        return
    joined = " | ".join(head)
    record(joined.startswith("event: endpoint") and "session_id" in joined,
           "MCP SSE 握手 (:8765/sse)", (joined[:120] or "读不到首帧") + suffix)


PRESTART_TOOL = "kb_list_sources"


async def _prestart_session(host: str, user: str, base: str, budget: float) -> tuple[list[str], str]:
    """在 :10003 上问一句话，只等 `<prestart` 出现，出现就立刻收工。

    不能复用 check_ws：它等的是「一句话说完」，而 prestart 那一整句是在模型开始
    生成之前就被写进流里的（force_first），看到就该走人 —— 多等的每一秒都是在
    烧本机 ollama 的排队，而那台机器同时还挂着别的实例。

    异常只带回来、不在这里记判据：这条判据的名字由调用方独占记录，两边都记会出现
    两行同名结果（一行异常、一行「没收到帧」），读日志的人分不清哪行才算数。
    """
    import websockets

    hits: list[str] = []
    try:
        async with websockets.connect(f"ws://{host}:10003",
                                      ping_interval=None, close_timeout=3) as ws:
            await ws.send(json.dumps({"Username": user, "Output": False}, ensure_ascii=False))
            http_post(base, "/api/send", {"username": user, "msg": "用一句话介绍你自己"})
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(),
                                                 timeout=max(0.5, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed:
                    break
                blob = raw if isinstance(raw, str) else repr(raw)
                if "<prestart" in blob.lower():
                    hits.append(blob)
                    break
    except Exception as exc:
        return hits, repr(exc)
    return hits, ""


def check_prestart_tool(base: str, host: str, user: str, budget: float) -> None:
    """一轮自然语言输入到底有没有真的驱动过 MCP 工具 —— 用 prestart 这条路量。

    check_mcp_tools 证的是「我们从 :5010 手动调，工具会返回」；模型会不会自己决定调
    工具是另一条 prompt 规划路（_call_planner_llm 吐 {"action":"tool"}），9b 都撑不到，
    所以 README 把它记成边界。prestart 是第三种情况：它在拼提示词之前无条件执行，
    不吃模型能力 —— llm/nlp_cognitive_stream.py:2374 `_run_prestart_tools(content)`
    → :462 → faymcp/runtime_bridge.py:53 → mcp_runtime.call_tool(skip_enabled_check=True)，
    结果在 :2664 以一整句 `<prestart>…</prestart>` 写进回答流。于是「一句话进来 →
    服务端进程内真的执行了一个 MCP 工具 → 输出进了这一轮」第一次变成可判的。

    观测点只有 :10003，而且是刻意选的：:10002 那侧 core/fay_core.py:2931
    `__remove_prestart_tags` 明写「不发送给数字人」，HTTP /api/get-msg 也被
    gui/flask_server.py:1407 剥掉，只有面板的 panelReply 走
    `__truncate_think_for_panel` —— 它只处理 think 标记，不含 think 时原样返回。
    更关键的是这条执行**不经 :5010**（runtime_bridge 直接进程内调），所以 :5010 上
    数不出请求、也伪造不出这个帧：判据要的就是这份不可伪造性。

    注册/注销必须成对：配置写在容器层的 faymcp/data/mcp_prestart_tools.json，
    漏注销会让这台实例之后每一轮都白白跑一次工具。
    """
    svc = f"http://{host}:5010"

    def get(path: str, timeout: float = 20.0) -> dict:
        with urllib.request.urlopen(svc + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def post(path: str, payload: dict, timeout: float = 20.0) -> dict:
        req = urllib.request.Request(
            svc + path, data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def runnable():
        """可运行预启动清单；读不出来返回 None —— 跟「清单是空的」是两件事，不能混。"""
        try:
            raw = get("/api/mcp/prestart/runnable")
            rows = raw.get("prestart_tools") if isinstance(raw, dict) else raw
            rows = rows or []
        except Exception:
            return None
        return [(int(r["server_id"]), str(r["tool"]))
                for r in rows if isinstance(r, dict) and r.get("server_id") and r.get("tool")]

    clean = "MCP 预启动工具注销后清单回到空"

    def disable_and_verify(sid: int) -> None:
        try:
            post(f"/api/mcp/servers/{sid}/tools/{PRESTART_TOOL}/prestart", {"enabled": False})
        except Exception as exc:
            record(False, clean, repr(exc))
            return
        left = runnable()
        record(left is not None and not left, clean,
               f"{sid}/{PRESTART_TOOL} 已摘掉" if left == [] else
               ("注销之后清单仍读不回来" if left is None else f"剩余 {left}"))

    # 挑一台「在线」且暴露 PRESTART_TOOL 的服务器：runnable 那份清单只认在线的
    # （mcp_service.py:1248 那句 status != "online" 直接 continue），拿离线的那台
    # 去注册只会得到一条永远为空的清单，量不到东西。
    # /servers 的返回形状不收口：有时是 {"servers": [...]}、有时直接是那个 list
    # （check_mcp_tools 为此早写了同样的两种都认），照 single-shape 写就会在这里
    # 抛 AttributeError: 'list' object has no attribute 'get'。
    target = None
    try:
        raw = get("/api/mcp/servers")
        servers = raw.get("servers") if isinstance(raw, dict) else raw
        servers = servers if isinstance(servers, list) else []
        for s in servers:
            if not isinstance(s, dict) or s.get("status") != "online":
                continue
            try:
                tools = get(f"/api/mcp/servers/{s['id']}/tools")
                names = [t.get("name") for t in (tools.get("tools") or []) if isinstance(t, dict)]
            except Exception:
                continue
            if PRESTART_TOOL in names:
                target = s["id"]
                break
    except Exception as exc:
        record(False, "MCP 预启动工具注册与可运行清单 (:5010)",
               f"挑不出「在线且暴露 {PRESTART_TOOL}」的服务器：{exc!r}")
        return
    if target is None:
        record(None, "MCP 预启动工具注册与可运行清单 (:5010)",
               f"在线的服务器里没有一台暴露 {PRESTART_TOOL}（在线的只有 "
               f"{[(s.get('id'), s.get('name'), s.get('status')) for s in servers]}）—— "
               "没有可用的预启动对象，这条判据根本没被执行")
        return

    name = "MCP 预启动工具注册与可运行清单 (:5010)"
    # 先看清单是不是已经脏了：上一轮探针被中途杀掉时注册会留在容器层
    # （faymcp/data/mcp_prestart_tools.json 跟着容器活），那下面那句「注册之后清单里
    # 有它」就成了空话 —— 残留本来就在里面，注册成功与否都判得出 PASS。
    stale = runnable()
    leftover = stale is not None and (target, PRESTART_TOOL) in stale
    if leftover:
        try:
            post(f"/api/mcp/servers/{target}/tools/{PRESTART_TOOL}/prestart", {"enabled": False})
        except Exception as exc:
            record(False, name, f"清单里本来就有它（上一轮漏注销的残留），而这次连注销都失败了：{exc!r}")
            return
    try:
        post(f"/api/mcp/servers/{target}/tools/{PRESTART_TOOL}/prestart",
             {"enabled": True, "params": {}, "include_history": True,
              "allow_function_call": False})
    except Exception as exc:
        record(False, name, repr(exc))
        return
    listed = runnable()
    registered = listed is not None and (target, PRESTART_TOOL) in listed
    record(registered, name,
           f"server_id={target} {PRESTART_TOOL} 已登记，"
           + ("runnable 清单里能看到它" if registered else
              ("清单读不回来" if listed is None else "但 /api/mcp/prestart/runnable 里没有它"))
           + ("（先清掉了上一轮漏注销的残留）" if leftover else ""))
    if not registered:
        # 没登记上就别往下走了：后面的会话只会因为「没有工具可跑」而拿不到帧，
        # 那条 SKIP 会把真正的故障（注册面）盖掉。注销仍然要做。
        disable_and_verify(target)
        return

    try:
        hits, error = asyncio.run(_prestart_session(host, user, base, budget))
    except Exception as exc:
        hits, error = [], repr(exc)
    if hits:
        blob = hits[0]
        start = blob.lower().find("<prestart")
        # 不假设它一定从 panelReply 出来：扫描的是整帧原文。:10003 上有 panelMsg
        # （prestart 会被 core/fay_core.py:2835 那条 if 跳过）和 panelReply（:2856）
        # 两个口子，究竟走了哪个、甚至走了第三个我们没料到的，都按实测写进详情。
        keys = ""
        try:
            keys = f"（帧顶层键 {sorted(json.loads(blob))}）"
        except Exception:
            pass
        record(True, "一轮对话真的执行了 MCP 工具（prestart 结果进回答流）",
               f":10003 收到的一帧里出现 {blob[start:start + 24]!r}…"
               + f"，内容是 {PRESTART_TOOL} 的真实输出{keys}"
               "（这条执行不经 :5010，进程内直调，所以不是探针自己替它调的）")
    else:
        latency_verdict(
            False, "一轮对话真的执行了 MCP 工具（prestart 结果进回答流）",
            f"{budget:.0f}s 内 :10003 没收到带 <prestart> 的帧"
            + (f"，{error}" if error else "")
            + f"｜注册已确认成功（server_id={target}），所以缺的是执行或推送那一段")

    disable_and_verify(target)


def check_llm_baseline(base: str, timeout: float) -> float:
    """量一次本地 LLM 的真实耗时，作为后续问答超时的标尺。

    故意不传 max_tokens：qwen3.5 这类带思考段的模型会把小额度全部花在
    reasoning 上，实测 max_tokens=16 时 68.8s 后 content 反而是空的
    （直接打 ollama /api/generate 也一样：eval_count=8、response=""）。
    让「只回一个字」这个 prompt 自己收尾，量到的才是链路真实耗时。

    model 固定 'fay' = Fay 自己的大脑（gui/flask_server.py:775 的兼容层），不另找上游：
    这条链路与下面 check_chat / check_ws 用的是同一个 LLM 出口，耗时才有可比性。
    """
    try:
        elapsed, text = openai_post(
            base + "/v1/chat/completions",
            {"model": "fay", "stream": False,
             "messages": [{"role": "user", "content": "只回一个字：好"}]},
            timeout,
        )
        ok, why = reply_verdict(text)
        latency_verdict(ok, "OpenAI 兼容层 /v1/chat/completions (model=fay)",
                        f"{elapsed:.1f}s: {why}")
        return elapsed
    except Exception as exc:
        latency_verdict(False, "OpenAI 兼容层 /v1/chat/completions (model=fay)", repr(exc))
        return 0.0


def http_post(base: str, path: str, payload: dict, timeout: float = 20) -> dict:
    body = urllib.parse.urlencode({"data": json.dumps(payload, ensure_ascii=False)}).encode()
    req = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def wait_http(base: str, seconds: float) -> bool:
    # /api/get-system-status 是 Fay 自带的 GET 健康口子（gui/flask_server.py:1115）。
    # 4xx 也算「HTTP 面活着」：urllib 会对非 2xx 抛 HTTPError，不能当成连不上。
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/get-system-status", timeout=3) as resp:
                if resp.status < 500:
                    return True
                time.sleep(2)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return False


def check_chat(base: str, user: str, timeout: float) -> str:
    """一问一答，返回回复文本（失败返回空串）。"""
    started = time.monotonic()
    try:
        before = http_post(base, "/api/get-msg", {"username": user, "limit": 5, "offset": 0})
        baseline = max([int(r["id"]) for r in before["list"]] or [0])
        http_post(base, "/api/send", {"username": user, "msg": "只回一个字：好"})
        signature: dict[int, int] = {}
        collected: dict[int, str] = {}
        last_growth = time.monotonic()
        while time.monotonic() - started < timeout:
            time.sleep(0.8)
            rows = http_post(base, "/api/get-msg", {"username": user, "limit": 20, "offset": 0})["list"]
            fresh = {
                int(r["id"]): r["content"].strip()
                for r in rows
                if r["type"] == "fay" and int(r["id"]) > baseline and (r["content"] or "").strip()
            }
            lengths = {k: len(v) for k, v in fresh.items()}
            if lengths != signature:
                collected, signature, last_growth = fresh, lengths, time.monotonic()
            elif collected and time.monotonic() - last_growth >= 2:
                break
        reply = "".join(collected[k] for k in sorted(collected))
        ok, why = reply_verdict(reply)
        latency_verdict(ok, "HTTP 问答链路 (/api/send → /api/get-msg)",
                        f"{time.monotonic() - started:.1f}s, {len(reply)} 字: {why}")
        return reply
    except Exception as exc:
        latency_verdict(False, "HTTP 问答链路 (/api/send → /api/get-msg)", repr(exc))
        return ""


def _has_substance(frame: dict) -> bool:
    """这一帧里到底有没有「回答的内容」—— 收工时机的唯一凭据。

    不能按「序列化后的帧里出现过 "text" 这个字符串」判：:10002 上会出现一帧
    `Key='text', Value='', IsFirst=1, IsEnd=1` 的终止帧（run #20 的 fay 组实测到），而
    `core/fay_core.py:2946` 的守卫正是「文本为空且不是结束标记才不发」，所以空帧
    一定带着 IsEnd=1。把它当成「开始回答了」，6s 静默窗口就会把会话关掉：run #20 的
    fay 组在 21:27:44 收工，同一发回答的句子 21:28:25 才落地，差了 41s。
    """
    if "panelReply" in frame:
        return bool(str(frame.get("panelReply") or "").strip())
    data = frame.get("Data")
    if isinstance(data, dict) and str(data.get("Key")).lower() == "text":
        return bool(str(data.get("Value") or "").strip())
    return False


def _text_blob(frames: list[dict]) -> str:
    """这个会话里所有非空 `Data.Key=text` 帧拼起来的正文（空终止帧不计）。"""
    return "".join(
        str(f["Data"].get("Value"))
        for f in frames
        if isinstance(f.get("Data"), dict)
        and str(f["Data"].get("Key")).lower() == "text"
        and str(f["Data"].get("Value") or "").strip()
    )


def _empty_text_shape(frames: list[dict]) -> str:
    """一帧正文都没有时，把 :10002 到底推了什么写清楚，供重试那条判据引用。"""
    keys = sorted({str((f.get("Data") or {}).get("Key")) for f in frames})
    ends = [(f["Data"].get("IsFirst"), f["Data"].get("IsEnd"))
            for f in frames
            if isinstance(f.get("Data"), dict)
            and str(f["Data"].get("Key")).lower() == "text"]
    return f"{len(frames)} 帧、Keys={keys}、text 帧 (IsFirst,IsEnd)={ends}"


async def check_ws(
    host: str, port: int, user: str, base: str, timeout: float, output: bool = False
) -> list[dict]:
    """连 WS、注册用户名，收集 timeout 秒内服务端推来的所有帧。

    host 必须取自 --base：探针自己也在 bridge 网络上，127.0.0.1 是它自己，
    数字人的 10002/10003 只有经服务名（fay / origin-fay）才到得了。
    """
    import websockets  # 容器内为上游钉住的 10.4（legacy 实现）

    frames: list[dict] = []
    url = f"ws://{host}:{port}"
    tag = f"WS :{port} Output={str(output).lower()}"
    try:
        async with websockets.connect(url, ping_interval=None, close_timeout=3) as ws:
            await ws.send(json.dumps({"Username": user, "Output": output}, ensure_ascii=False))
            print(f"  (已连接 {url} 并注册 {user})", flush=True)
            http_post(base, "/api/send", {"username": user, "msg": "用一句话介绍你自己"})
            deadline = time.monotonic() + timeout
            # 这个会话还欠服务端一个 audio 帧：Output=true 且一帧 audio 都没收到。
            # 只有欠着的时候才允许把会话整体延长 AUDIO_GRACE_SECONDS（见常量注释）。
            expects_audio = output
            answered = False
            seen_audio = False
            hard = deadline + (AUDIO_GRACE_SECONDS if expects_audio else 0.0)
            while time.monotonic() < hard:
                # 回答前必须一直等：question/log 帧先到，LLM 要 5~40s 才吐 text 帧，
                # 一静就收工会把 text 帧漏掉。收到答案后再等一个静默窗口才算说完，
                # 窗口长度按「还在欠 audio」区分：欠着给 45s，否则 6s 足够。
                remaining = hard - time.monotonic() if answered else deadline - time.monotonic()
                if remaining <= 0:
                    break
                quiet = AUDIO_GRACE_SECONDS if expects_audio and not seen_audio else 6.0
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=min(quiet if answered else remaining, remaining))
                except asyncio.TimeoutError:
                    if answered:
                        break
                    continue
                except websockets.exceptions.ConnectionClosed:
                    # 服务端主动断开：已经收到的帧仍然是证据，不能因为连接没了就把
                    # 整段会话当成「一帧都没收到」（下面 record 用的是 frames，不是异常）
                    break
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    frame = {"_raw": str(raw)[:80]}
                frames.append(frame)
                data = frame.get("Data")
                if isinstance(data, dict) and str(data.get("Key")).lower() == "audio":
                    seen_audio = True
                if not answered:
                    answered = _has_substance(frame)
        record(bool(frames), f"{tag} 注册并收到服务端帧", f"{len(frames)} 帧")
    except Exception as exc:
        record(False, f"{tag} 注册并收到服务端帧", repr(exc))
    return frames


def summarize_frames(frames: list[dict], port: int, output: bool,
                     attempt_note: str = "") -> None:
    """10002 是 human/UE 通道，契约是 {Topic, Data:{Key, Value}}；
    10003 是 web 面板通道，推的是 panelMsg / panelReply / robot 这类扁平字段
    （core/wsa_server.py 的 __producer 按 Topic 分流，两条通道不同形）。"""
    top_keys: set[str] = set()
    topics: set[str] = set()
    keys: set[str] = set()
    has_text = has_audio = has_panel = has_lips = False
    empty_text: list[dict] = []
    for f in frames:
        top_keys.update(str(k) for k in f)
        topics.add(str(f.get("Topic")))
        data = f.get("Data") or {}
        if isinstance(data, dict):
            keys.add(str(data.get("Key")))
            value = data.get("Value")
            if data.get("Key") == "text" and isinstance(value, str) and value.strip():
                has_text = True
            elif data.get("Key") == "text":
                empty_text.append(data)
            if value and str(data.get("Key", "")).lower() in ("audio", "voice"):
                has_audio = True
        if any(k in ("panelMsg", "panelReply") for k in f):
            has_panel = True
        if "Lips" in json.dumps(f, ensure_ascii=False):
            has_lips = True

    sample = json.dumps(frames[0], ensure_ascii=False)[:160] if frames else "无"
    if port == 10003:
        record(
            has_panel,
            "WS :10003 面板契约 (panelMsg/panelReply)",
            f"顶层键={sorted(top_keys)} 样例={sample}",
        )
        return

    record(
        bool(topics - {"None"}),
        "WS :10002 human 契约含 Topic/Data",
        f"Topics={sorted(topics)} Keys={sorted(keys)}",
    )
    blob = _text_blob(frames)
    if has_text:
        ok, why = reply_verdict(blob)
    elif empty_text:
        # 区分「一帧 text 都没有」和「只有空 text」：前者是时机/断连问题，后者是
        # 管线这一轮真的没产出内容（本机 9b 的吐空轮就是这个形状）。
        ok, why = False, (f"{_empty_text_shape(frames)} —— 只有空内容的 text 终止帧"
                          f"（IsFirst,IsEnd），一个字都没播")
    else:
        ok, why = False, "一帧 text 都没收到"
    latency_verdict(ok, "WS :10002 收到文字播报 (Data.Key=text)",
                    (f"{len(blob)} 字: {why}" if has_text else why) + attempt_note)
    # 音频支路的开关是**客户端注册时声明的 Output**，不是服务端的 playSound：
    # core/fay_core.py:2212 用 wsa_server.get_client_output(username) 决定是否往
    # 10002 推 audio 帧，:1568 更进一步 —— 只要有客户端声明要音频输出就走 TTS 合成。
    # 容器侧的 playSound / automatic_player_status 只管 Fay 自己那块（不存在的）声卡。
    # 两个方向不对称，所以分开记：
    #   Output=false 还收到 audio  == 服务端漏推，与 LLM 无关 → 必须 FAIL
    #   Output=true  但没收到      == 得先有回复才有音频可合成。run #4 这条 FAIL 的
    #                                详情是 Keys=['log','question']（一个字都没播），
    #                                属于同一条 LLM 依赖的重复证据 → 按环境降级
    if output:
        if not has_audio and TTS_OK is False:
            # 合成这一发在本轮同一时刻就是失败的（见 TTS_OK 的注释）：to_sample() 返回
            # None，服务端日志跟着出现「合成音频完成. 耗时: 8018 ms 文件:None」和
            # 「digital human audio end queued」（对照成功那次的「文件:./samples/xxx.wav」
            # + 「digital human audio sent」），于是一帧 audio 都没有。
            # 这不是容器化坏了，判红只会把外部依赖的抖动记成我们的回归。
            record(
                None,
                "WS :10002 音频帧跟随客户端 Output (本会话 Output=true)",
                f"期望 audio 帧=True，实际收到=False（Keys={sorted(keys)}）；"
                "同一轮 check_tts 也合成失败 => 本机到 speech.platform.bing.com 的出口不通，"
                "这是外部依赖，不是容器化回归",
            )
        else:
            latency_verdict(
                has_audio,
                "WS :10002 音频帧跟随客户端 Output (本会话 Output=true)",
                "收到 audio 帧" if has_audio
                else f"期望 audio 帧=True，实际收到=False（Keys={sorted(keys)}）",
            )
    else:
        record(
            not has_audio,
            "WS :10002 音频帧跟随客户端 Output (本会话 Output=false)",
            "" if not has_audio else f"客户端没声明要音频，服务端却推了 audio 帧（Keys={sorted(keys)}）",
        )
    record(not has_lips, "WS :10002 无 Lips 字段（Linux 侧预期）",
           "上游口型生成只在 Windows 分支产出" if not has_lips else "收到 Lips")


def check_audio_url(frames: list[dict], base: str) -> None:
    """数字人客户端拿音频的真实取法：audio 帧里的 HttpValue 是个 HTTP URL
    （core/fay_core.py:2239 拼的 f'{cfg.fay_url}/audio/' + basename）。

    cfg.fay_url 默认写 127.0.0.1，从探针容器看那是它自己，所以只取 path，
    host 用 --base 重组。Value 那个绝对路径是 Fay 容器内的，跨容器没有意义。
    """
    for f in frames:
        data = f.get("Data") or {}
        if isinstance(data, dict) and data.get("Key") == "audio" and data.get("HttpValue"):
            path = urllib.parse.urlparse(str(data["HttpValue"])).path
            url = base.rstrip("/") + path
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    size = len(resp.read())
                record(size > 1000, "HTTP 取到 audio 帧指向的音频文件", f"{url} {size} 字节")
            except Exception as exc:
                record(False, "HTTP 取到 audio 帧指向的音频文件", f"{url} {exc!r}")
            return
    # 走到这里 == 会话里一帧带 HttpValue 的 audio 都没有。上面那条判据已经按环境降级了，
    # 这条是它的直接下游（没有 audio 帧就没有 URL 可取），不能独立算容器故障。
    if TTS_OK is False:
        record(None, "HTTP 取到 audio 帧指向的音频文件",
               "出口不通（见 check_tts 那条）=> 没有 audio 帧，也就没有 URL 可取")
    else:
        latency_verdict(False, "HTTP 取到 audio 帧指向的音频文件",
                        "会话里没有带 HttpValue 的 audio 帧")


# 远程音频输入口（TCP 10001）的协议：客户端连上后可以先发一帧
#   <username>xxx</username>
# 把自己注册成某个用户，之后写的字节一律被当作 16kHz 单声道 int16 PCM
# （fay/fay_booter.py:131 DeviceInputListener.run 的 recv 循环 -> streamCache）。
# 值得单独量的原因是 core/recorder.py:251 那一行：
#   if not record['enabled'] and not self.is_remote(): continue
# 麦克风那条路被 config 关掉时，远程这条路**照样在跑**（DeviceInputListener.is_remote()
# 恒为 True），而且 accept_audio_device_output_connect 在 fay_booter.py:421 是无条件起的。
# 所以「容器无声卡 => 语音输入整条不会触发」只对麦克风成立。这里就把这句话从推测
# 改成实测：把 PCM 推到 10001，看服务端的 VAD 认不认。
REMOTE_AUDIO_PORT = 10001


async def _remote_audio_session(host: str, user: str) -> list[dict]:
    """注册一个 human WS 客户端 + 一路 10001 远程麦克风，喂 4 秒满幅方波再喂 1.2 秒静音。

    方波是为了跨过 VAD 的动态阈值（recorder.py:287 比的是 rms(data,2)/25000，初始阈值 0.5，
    而阈值在收到第一帧安静数据时会一路掉到 0.02），静音是为了让「说话结束」成立
    （_RELEASE=0.5s 之后才走 ASR）。节奏按 16k/单声道/int16 的实时速率推，不要一次灌完，
    那样测的是 StreamCache 的容量而不是 VAD。
    """
    import websockets

    frames: list[dict] = []
    loud = b"\x00\x40\x00\x40" * 256          # 1024 字节 = 512 个 +16384 的采样
    quiet = b"\x00\x00" * 512                 # 同长度的静音
    async with websockets.connect(f"ws://{host}:10002", ping_interval=None,
                                 close_timeout=3) as ws:
        await ws.send(json.dumps({"Username": user, "Output": False}))
        # 只写不读：服务端每 10s 发一个 9 字节心跳（fay_booter.py:203），不收也不会堵
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, REMOTE_AUDIO_PORT), timeout=10)
        try:
            writer.write(f"<username>{user}</username>".encode())
            await writer.drain()
            for i in range(125):              # 125 x 1024B / 32000B/s ≈ 4s
                writer.write(loud if i < 122 else quiet)
                await writer.drain()
                await asyncio.sleep(0.032)
            await asyncio.sleep(1.2)          # 再补一段静音，坐实"这句话说完了"
            deadline = time.monotonic() + 12  # ASR 侧 funasr 的等待上限是 10s
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(),
                                                 timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed:
                    break
                try:
                    frames.append(json.loads(raw))
                except (ValueError, TypeError):
                    frames.append({"_raw": str(raw)[:80]})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    return frames


def check_remote_audio_input(host: str) -> None:
    """语音输入：TCP 10001 推 PCM -> 服务端 VAD -> ASR。

    拆成两条记，因为「管道通」与「认得出字」是两个独立的事实：
      1. VAD 收到事件（:10002 上出现 Data.Key=log 且 Value 含「聆听中」）—— 这条与
         显存、与 ASR 后端都无关，只要容器化没把 10001 弄坏就必然成立，不过就是 FAIL。
      2. 识别出文字（ASR 把 finalResults 当 log 帧推回来）—— 本栈的 ASR 是
         `ASR_mode=funasr` + `local_asr_ip=host.docker.internal:10197`，也就是一个
         需要另外起的 FunASR 服务；它不在线时这条判不了，按 SKIP 记并写明缺什么。

    实测（2026-09-20，两个 9b 实例 + 一个 lite 实例）：打上
    patches/{fay,origin_fay}/0004-remote-audio-*.patch 之后 1 是 PASS、2 是 SKIP；
    打之前 origin-fay 连 10001 都握不上（上游 74b49ae 的线程竞态），fay 能连上但
    每条断开的连接留一个 fd 和两个线程。详见 README「远程音频输入：无声卡也能测」。
    """
    user = f"probe_mic_{int(time.time())}"
    try:
        frames = asyncio.run(_remote_audio_session(host, user))
    except Exception as exc:
        record(False, f"TCP :{REMOTE_AUDIO_PORT} 远程音频输入口连得上", repr(exc))
        return
    record(True, f"TCP :{REMOTE_AUDIO_PORT} 远程音频输入口连得上并注册了用户名", user)

    logs = []
    for f in frames:
        data = f.get("Data") or {}
        if isinstance(data, dict) and str(data.get("Key")) == "log":
            logs.append(str(data.get("Value")))
    heard = any("聆听中" in v for v in logs)
    record(heard, "远程 PCM 跨过了服务端的 VAD（recorder 判定有人在说话）",
           f"12s 内收到 log 帧 {len(logs)} 条：{logs[:4] or '无'}"
           + ("" if heard else "｜服务端没把这段声音当作语音。三种可能，按代码位置排："
                               "① 10001 上没收到字节（连接被服务端线程提前关掉，"
                               "参见 patches/fay/0004 修的那个竞态）；"
                               "② recorder 的远程支路没在跑（core/recorder.py:251 的 is_remote 免检）；"
                               "③ 拾音被丢弃 —— wake_word_enabled=false 时只要 FeiFei.speaking "
                               "为 True，读到的帧就直接扔掉（core/recorder.py:267），"
                               "而 speaking 是全局的、与用户名无关，"
                               "所以这条判据必须排在任何会播报的问答之前"))
    recognized = [v for v in logs if v and "聆听中" not in v]
    if recognized:
        record(True, "远程音频的 ASR 认出了文字", f"{recognized[:2]}")
    elif not heard:
        record(None, "远程音频的 ASR 认出了文字", "VAD 都没过，识别无从谈起")
    else:
        record(None, "远程音频的 ASR 认出了文字",
               "VAD 已过、识别结果为空：这条链路的 ASR 后端是 ws://host.docker.internal:10197"
               "（overlay/fay/system.conf 的 ASR_mode=funasr），本机没有起 FunASR 服务，"
               "与容器化无关")


GENAGENTS_PORT = 5001
# instruction 会原样出现在 templates/decision_interview.html:75 的 {{ instruction }} 里，
# 所以拿它当"这一页是刚为本次请求渲染的"证据，而不是某个上次留下的页面。
GENAGENTS_INSTRUCTION = "probe-决策面谈-评估一次随访安排的可行性"


def _post_json(url: str, payload: dict, timeout: float = 20.0) -> tuple[int, str]:
    """真正的 JSON POST —— 不能复用 http_post()。

    http_post() 发的是 `data=<urlencoded json>` 表单，那是 /api/send 那一组口的约定；
    而 /api/start-genagents 读 request.get_json()（gui/flask_server.py:1721），
    表单体在它眼里是 None，只会得到 400「缺少克隆要求参数」。
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def wait_port_closed(host: str, port: int, wait: float) -> float:
    """等到 port 不再监听，返回等待秒数；超时返回 -1。与 wait_port 相反。"""
    start = time.monotonic()
    while time.monotonic() - start < wait:
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            if sock.connect_ex((host, port)) != 0:
                return time.monotonic() - start
        finally:
            sock.close()
        time.sleep(0.5)
    return -1.0


def check_genagents_interview(base: str, host: str, timeout: float = 20.0) -> None:
    """genagents 决策面谈（人格克隆）服务：:5001 起得来、页面渲得出、收尾关得掉。

    这条此前只是 README 里一句「只有 5001 没起」的观察。读源码才知道它本来就不该在
    开机时监听：`POST /api/start-genagents`（gui/flask_server.py:1711）要求
    `fay_booter.is_running()`，然后 start_genagents_server() 只做 setup()（建目录、
    必要时把模板拷进 templates/）就返回 app，由 werkzeug make_server('0.0.0.0', 5001)
    在守护线程里 serve —— 所以这一档**不花 LLM**，量的是纯容器化事实：镜像里有没有
    templates/ 那 34KB、genagents 包 import 不 import 得动、:5000 管理口在容器里
    能不能拉起一个子服务。

    收尾必须自己关掉：:5001 上 `POST /api/shutdown` 只置 shutdown_flag
    （genagents_flask.py:192），由 flask_server.py:1778 那个 monitor 线程去
    `server.shutdown()`。不关就等于给后面的组留一个没人预期的端口。
    """
    try:
        code, raw = _post_json(base + "/api/start-genagents",
                               {"instruction": GENAGENTS_INSTRUCTION}, timeout)
    except Exception as exc:
        record(False, "决策面谈服务启动 (:5000 POST /api/start-genagents)", repr(exc))
        return
    try:
        started = json.loads(raw)
    except Exception:
        record(False, "决策面谈服务启动 (:5000 POST /api/start-genagents)",
               f"HTTP {code} 但不是 JSON：{raw[:120]!r}")
        return
    if started.get("success") is not True or "5001" not in str(started.get("url", "")):
        record(False, "决策面谈服务启动 (:5000 POST /api/start-genagents)",
               f"HTTP {code} 回报={started}")
        return
    waited = wait_port(host, GENAGENTS_PORT, wait=20.0)
    if waited < 0:
        record(False, f"决策面谈服务 :{GENAGENTS_PORT} 开始监听",
               "启动接口说成功了，端口等 20s 仍没 bind 上")
        return
    record(True, "决策面谈服务启动 (:5000 POST /api/start-genagents)",
           f"HTTP {code} 回报 {started.get('url')!r}，:{GENAGENTS_PORT} 在 {waited:.1f}s 内 bind")
    try:
        with urllib.request.urlopen(f"http://{host}:{GENAGENTS_PORT}/", timeout=timeout) as resp:
            page_code, html = resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        record(False, "决策面谈页 :5001/ 渲染 (decision_interview.html)", repr(exc))
        return
    good = page_code == 200 and GENAGENTS_INSTRUCTION in html and "instructionText" in html
    record(good, "决策面谈页 :5001/ 渲染 (decision_interview.html)",
           f"HTTP {page_code}，{len(html)} 字节"
           + ("" if good else f"；页面里没有刚提交的 instruction（{GENAGENTS_INSTRUCTION!r}）"))
    try:
        _post_json(f"http://{host}:{GENAGENTS_PORT}/api/shutdown", {}, timeout=10)
    except Exception as exc:
        record(False, f"决策面谈服务收尾：:{GENAGENTS_PORT} 关回不监听", f"shutdown 口调用失败 {exc!r}")
        return
    closed = wait_port_closed(host, GENAGENTS_PORT, wait=15.0)
    record(closed >= 0, f"决策面谈服务收尾：:{GENAGENTS_PORT} 关回不监听",
           f"monitor 线程在 {closed:.1f}s 内 release 了端口" if closed >= 0
           else "等 15s 端口仍在监听 —— 给后面的组留了个没人预期的口")


# check_tts 的结论。audio 帧那两条判据要靠它把两种完全不同的事故分开：
#   TTS_OK=False  -> 本机到 speech.platform.bing.com 的出口此刻不通（run #16 实测：
#                     容器日志 [x] 原因: Cannot connect to host speech.platform.bing.com:443
#                     [Temporary failure in name resolution]），服务端 to_sample() 只能返回
#                     None，数字人自然收不到 audio 帧 —— 这是外部依赖，不是容器化坏了；
#   TTS_OK=True   -> 同一轮里出口明明是通的，那 audio 帧缺失就是真缺陷，必须 FAIL。
# 所以 check_tts 必须跑在 WS 会话之前，否则这个判别只能事后诸葛亮。
TTS_OK: bool | None = None


def check_tts() -> None:
    """直接调 ms_tts_sdk，验 edge_tts 是否真能出网合成并落地 wav。

    注意 to_sample 的返回契约是**文件路径字符串**（成功）或 None（失败），
    不是音频字节 —— 量长度必须量文件大小，否则永远判定失败。
    结论同时写进 TTS_OK，供后面 audio 帧那两条判据决定该 FAIL 还是 SKIP。
    """
    global TTS_OK
    sys.path.insert(0, os.getcwd())
    try:
        from utils import config_util as cfg

        cfg.load_config()
        from tts.ms_tts_sdk import Speech

        speech = Speech()
        result = speech.to_sample("你好，我是探针。", "cheerful")
        if not isinstance(result, str):
            TTS_OK = False
            # 只印「返回 None」会把下一次排查送去读 Fay 的源码：出口不通、音色改名、配额
            # 用完都会走到这一句。原因是 Fay 自己吞掉的 —— tts/ms_tts_sdk.py:116 那个
            # `except Exception` 只 `util.log` 出「[x] 原因: …」，不往上抛，所以探针拿不到。
            record(False, "TTS edge_tts 合成 (tts/ms_tts_sdk.py)",
                   f"返回 {result!r}｜原因被 ms_tts_sdk 吞在 except 里，只打到本容器 stdout 的"
                   "「[x] 原因: …」那一行，去看它")
            return
        size = os.path.getsize(result) if os.path.exists(result) else -1
        TTS_OK = size > 1000
        record(
            size > 1000,
            "TTS edge_tts 合成 (tts/ms_tts_sdk.py)",
            f"{result} {size} 字节" if size > 0 else f"{result} 文件缺失/过小(size={size})",
        )
        for path in {result, result.rsplit(".", 1)[0] + ".mp3"}:
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception as exc:
        TTS_OK = False
        record(False, "TTS edge_tts 合成 (tts/ms_tts_sdk.py)", repr(exc))


def check_embedding() -> None:
    """验仿生记忆用的向量到底是真向量，还是上游那条静默兜底的伪向量。

    `simulation_engine/gpt_structure.py:294-298`：API embedding 一失败就换成本地伪向量
    （`_create_mock_embedding`：sha256(text) 当随机种子 → uniform(-1,1) → L2 归一），
    流程不断、只在 stdout 留一行 print，于是仿生记忆「看起来在工作、存的却是假坐标」。
    这条判据是被自己逼出来的：把 EMBEDDING_TIMEOUT 压到 20s 之后，ollama 换入 embedding
    模型实测要 72.9s，run #7 里每一条问答都在用伪向量而没人察觉。
    辨伪不靠猜：拿同一个文本把那条兜底路径原样再算一遍，逐维比对。
    """
    name = "仿生记忆向量真伪 (get_text_embedding)"
    sys.path.insert(0, os.getcwd())
    text = "探针：今天天气不错，适合散步。"
    try:
        from utils import config_util as cfg

        cfg.load_config()
        from simulation_engine import gpt_structure as gs

        base, model = cfg.embedding_api_base_url, cfg.embedding_api_model
        started = time.monotonic()
        vec = gs.get_text_embedding(text)
        elapsed = time.monotonic() - started
    except Exception as exc:
        record(False, name, repr(exc))
        return
    if not isinstance(vec, (list, tuple)) or not vec:
        record(False, name, f"返回 {type(vec).__name__}，长度 {len(vec or [])}")
        return
    dim = len(vec)
    budget = (f"出口 {base} model={model}，"
              f"EMBEDDING_TIMEOUT={os.getenv('EMBEDDING_TIMEOUT') or '未设(上游默认60)'}/"
              f"{os.getenv('EMBEDDING_MAX_RETRIES') or '未设(上游默认2)'}")
    if all(abs(float(x)) < 1e-12 for x in vec):
        latency_verdict(False, name, f"{dim} 维全零，API 与伪向量两条路都没走出来｜{budget}")
        return
    mock = gs._get_mock_embedding_function(dim)(text)
    same_dim = len(mock) == dim
    fake = same_dim and max(abs(float(a) - float(b)) for a, b in zip(vec, mock)) < 1e-9
    if fake:
        latency_verdict(False, name,
                        f"{dim} 维但与上游兜底算法逐维一致 —— 这是伪向量，记忆质量是假的｜{budget}"
                        f"，本次 {elapsed:.1f}s")
    else:
        record(True, name,
               f"真向量（与兜底算法不一致，首维差 {abs(float(vec[0]) - float(mock[0])):.3g}）："
               f"{dim} 维、{elapsed:.1f}s｜{budget}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="单次问答的下限预算（秒）；实际预算按 LLM 基线耗时放大，见 --max-timeout")
    ap.add_argument("--max-timeout", type=float, default=420.0,
                    help="单次问答预算的天花板（秒），与 compose 里 LLM_REQUEST_TIMEOUT=420 对齐")
    ap.add_argument("--skip-tts", action="store_true")
    args = ap.parse_args()

    user = f"probe_{int(time.time())}"
    host = urllib.parse.urlparse(args.base).hostname or "127.0.0.1"
    if not wait_http(args.base, 120):
        record(False, "Fay HTTP 就绪", args.base)
        return 1
    record(True, "Fay HTTP 就绪 (/api/get-system-status)", args.base)
    # 与 LLM 无关、且只由容器化本身决定的两条，放在最前面：控制台静态资源挂了不该
    # 等到显存排队之后才发现。
    check_console(args.base)

    # 先取环境证据再判功能：直连 LLM 的下限耗时决定了后面几条能不能判、判成什么。
    check_llm_host(args.base, min(args.max_timeout, 240.0))
    # 与 LLM 无关的一条，放在预算测量之前：它确定性地证明 MCP 工具链是通的。
    check_mcp_tools(args.base)
    check_mcp_sse(host)
    # 语音输入也放在这一段，且必须在任何问答判据之前：recorder 在
    # `wake_word_enabled=false` 且 Fay 正在播音时会丢弃拾音（core/recorder.py:267），
    # 晚跑就会被前面那些问答的回复挡住，量出来的是时序而不是能力。
    check_remote_audio_input(host)
    # 决策面谈那条放在预算测量之前、与 LLM 无关这一段里：它只验"镜像里那套模板与
    # genagents 包能不能在容器里拉起一个子服务"，不需要等显存。
    check_genagents_interview(args.base, host)

    # 问答链路的耗时几乎全部花在本地 Ollama 上，而 llama-server 是整机共享的：
    # 实测同一句「用一句话介绍你自己」在空闲机器上 4.8s、在 16 核被压满时 >90s。
    # 固定 90s 会把「机器忙」误判成「Fay 坏了」，所以先量一次基线再定预算：
    # 基线问的就是「只回一个字」= 这条链路能给的最短回答，WS 那几问要的是一句话，
    # 实测约 2~4 倍，故取 4 倍，上下各夹一次。
    # 第一条的预算还按直连实测（DIRECT_LLM_SECONDS）夹一道：绕过 Fay 打 Ollama 都要
    # 39s 的机器上，等到 max_timeout 才失败只是白烧墙钟时间（run #3 实测：直连 39.1s，
    # 后面每条问答都撞满 600s 客户端超时）。
    # 降级态反而把这一条放宽到 8 倍：基线这一问走的是与被测容器完全同一条管线，
    # 是「这台机器上这个实例到底还能不能出真回复」最值钱的一条证据，值得等
    # （实测 origin-fay + 9b 的一句话要 172~301s，37.9s×8=303s 正好盖住）；
    # 后面的 /api/send 与三条 WS 是同一条根管线的重复证据，按 --timeout 快速过掉，
    # 真实帧契约由 compose 里的轻模型实例 fay-lite 兜住。
    first_mult = 8 if DEGRADED_LLM_HOST else 4
    first = min(args.max_timeout, max(args.timeout, (DIRECT_LLM_SECONDS or 0.0) * first_mult))
    baseline = check_llm_baseline(args.base, first)
    if DEGRADED_LLM_HOST:
        budget = args.timeout
    else:
        budget = min(args.max_timeout, max(args.timeout, baseline * 4))
    print(f"[probe] 单次问答预算 {budget:.0f}s（--timeout 下限 {args.timeout:.0f}s，"
          f"直连下限 {(DIRECT_LLM_SECONDS or 0.0):.1f}s，LLM 基线 {baseline:.1f}s ×4，"
          f"上限 {args.max_timeout:.0f}s）"
          + (f"；本机 LLM 已降级，只有基线那一条按 ×{first_mult}={first:.0f}s 等真回复，"
             "其余问答类判据按 --timeout 快速判 SKIP" if DEGRADED_LLM_HOST else ""),
          flush=True)

    check_chat(args.base, user, budget)
    # 「一句话进来到底有没有驱动工具」排在 :10002/:10003 那个会话循环之前：它会给
    # 这个实例临时注册一个预启动工具（配置落在容器层 faymcp/data/mcp_prestart_tools.json），
    # 函数自己在返回前注销；排在后面就会让那几轮问答也带上工具输出，量到的不再是原契约。
    check_prestart_tool(args.base, host, f"{user}_pre", budget)
    # TTS 挪到这些会话之前：它只出网、不碰显存，而它的结论要能解释后面 audio 帧那条判据
    # 为什么缺（见 TTS_OK）。原先它排在会话之后，run #16 那次「本机 DNS 到
    # speech.platform.bing.com 瞬断」就只能被记成两条属于我们的 FAIL。
    if not args.skip_tts:
        check_tts()
    for port, output in ((10003, False), (10002, False), (10002, True)):
        name = f"{user}_ws{port}{int(output)}"
        frames = asyncio.run(check_ws(host, port, name, args.base, budget, output))
        note = ""
        # :10002 上偶尔一整轮只推一帧空正文的 text 终止帧。一次吐空不该把整套容器判红
        # —— 但也不该被悄悄盖掉，所以这里明确再开一个会话量第二发，并把两发各自的形状写进
        # 同一条判据的详情里。两发都空仍然判红。重问前先等 EMPTY_REPLY_DRAIN_SECONDS，
        # 依据（含"为什么会吐空"的实测成因）写在那条常量的注释里。
        if port == 10002 and not _text_blob(frames):
            shape = _empty_text_shape(frames)
            time.sleep(EMPTY_REPLY_DRAIN_SECONDS)
            frames2 = asyncio.run(check_ws(host, port, f"{name}_r2", args.base, budget, output))
            blob2 = _text_blob(frames2)
            note = (f"｜第 1 发正文为空 [{shape}]，等 {EMPTY_REPLY_DRAIN_SECONDS:.0f}s "
                    f"让上一轮遗留的后台工具链释放显存后重问，"
                    + (f"第 2 发（换用户名）拿到 {len(blob2)} 字，判据按第 2 发计"
                       if blob2 else f"第 2 发仍空 [{_empty_text_shape(frames2)}]：两发都是空的"))
            if blob2:
                frames = frames2
        if frames:
            summarize_frames(frames, port, output, note)
            if port == 10002 and output:
                check_audio_url(frames, args.base)
    # 放在最后：它和问答共用同一个 ollama 出口，早跑会来抢本就紧张的显存排队，
    # 而这条判据本身不产出问答链路需要的东西。
    check_embedding()

    # None 是 SKIP，不是失败：`if not r[0]` 会把跳过一起算进失败，必须按 is False / is None 分。
    failed = [r for r in RESULTS if r[0] is False]
    skipped = [r for r in RESULTS if r[0] is None]
    print(f"\n[probe] {len(RESULTS) - len(failed) - len(skipped)}/{len(RESULTS)} 通过"
          + skip_tail(skipped), flush=True)
    for _, name, detail in failed:
        print(f"  ✗ {name}  {detail}", flush=True)
    for _, name, detail in skipped:
        print(f"  ○ {name}  {detail}", flush=True)
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
