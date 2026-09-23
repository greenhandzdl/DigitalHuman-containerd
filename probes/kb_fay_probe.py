#!/usr/bin/env python3
r"""知识库的真实业务判据：从 Fay 的业务口问一句话，证据取那一问的**原始回帧**。

为什么不直调工具（那是 probes/kb_ingest.py 与 probes/yueshen_probe.py 的活）：
那两个走 Fay :5010 的 MCP 管理面，证明的是「这台工具连得上、查得回来」。它们**看不见**
业务真正依赖的那件事 —— 一次用户提问里，知识库的话有没有被注入、有没有被模型用进回答。
这一组走 `/api/send` + 轮询 `/api/get-msg`（adapter 吃的就是这两个口），只看那一行的原文。

观测点为什么必须是 `/api/get-msg` 而不是那个 OpenAI 兼容口：`<prestart>` 块在 Fay 里有
三处出口，两处被明写剥掉 —— :10002 数字人侧 core/fay_core.py:2535
`__remove_prestart_tags`（注释就是「不发送给数字人」），GUI 侧
gui/flask_server.py:1275 与 :1407 那两条 `re.sub(r'<prestart>[\s\S]*?</prestart>', '', …)`
管的是 `/v1/chat/completions` 的流式与非流式回包。只有落库那一行是原文，
`/api/get-msg` 直接把 `content` 原样取出。所以这里读得到、那边读不到，不是探针不稳定。

原文里有什么（实测 2026-09-23，`血压的正常参考值是多少？`，12.0s 一发）：

    <prestart>【query_yueshen】(query=血压的正常参考值是多少？, top_k=3)
    〔健康监测数据〕指标：血压；分类：正常血压；参考标准 (诊室测量)：收缩压 < 120 mmHg …
    </prestart>对于成年人来说，在诊室测量时，收缩压（高压）小于 120 mmHg …
    <dh-end>

**这条链上"查不查知识库"不是大模型决定的。** `query_yueshen` 注册成了 prestart 工具
（`:5010/api/mcp/prestart/runnable` 读到 `{"query":"{{question}}","top_k":3}`，
`allow_function_call:false`）—— 每一轮说话之前无条件执行一次，把检索结果拼进提示词。
所以这里判的是注入这条路，别再拿它去证"模型学会查库了"。

与 fay_probe.check_prestart_tool 的分工，别看名字重了以为重复：那条是**注册一个临时
prestart 工具（kb_list_sources）再注销**，观测点是 :10003 的 WS 首帧，证的是「一句话进来
→ 服务端进程内真的执行了一个 MCP 工具」这个机制在容器里成立；这一组判的是**业务上长期
注册着的那台 query_yueshen**，而且判到注入之后：库里的话进了正文没有、把注册摘掉业务会不会
变。两条都要，一条讲机制通、一条讲业务用得上。

判据（每条都能被单独弄坏；第 6 条就是专门用来弄坏第 2、3 条的）：

  1. prestart 清单里就是 query_yueshen，且参数模板是 `{{question}}` —— 顺手把这份注册
     原样抓下来，第 7 条按它恢复，不按常量猜
  2. 这一问真的驱动了它 —— 回帧里有 prestart 注入块，且块头 `query=` 逐字等于本轮那一问
     （逐字比是为了防串轮：上一轮那块的残留也会长成一模一样的样子）
  3. 注入块里是**带出处标记 `〔…〕` 的语料片段**，且含 `--want` 那个词。这条是硬判据：
     注入块是工具输出的原文，中间没有采样，所以灌错语料、top_k 改坏、检索跑错集合都会当场红
  4. 这一问真的答出来了（`</prestart>` 之后的正文不是一句空话）。另有一条**软证据**：
     正文里复现了 `--want`。软的那条不计红（没复现记 SKIP，不记 FAIL），是连着三跑逼出来的：
     注入块里明明有「家庭自测血压的高血压诊断标准为 ≥ 135/85 mmHg」，同一问、同一份注册、
     同一个 top_k，三次里只有第二次正文写了 135/85，第一次和第三次答的是模型自己的通用血压
     常识（「收缩压小于 120…120–139/80–89、≥140/90」），一个 135 也没提，可话说得完完整整。
     复现率 1/3，判红就是把采样噪声写进 CI
  5. 回帧以 `<dh-end>` 收尾（fay/0008 那条哨兵契约在这条链上也得成立，否则 adapter
     要白等 8s 静默才收工），并把注入字数、正文字数与本轮墙钟一起打出来
  6. 反向对照两问：(a) 同一行里那个语料中不存在的假词必须不出现 —— 「含词」这条判据得有
     分辨力，不能换什么词都算命中；(b) **把 query_yueshen 从 prestart 注册里摘掉**
     （`POST :5010/api/mcp/servers/<id>/tools/<tool>/prestart` `{"enabled": false}` →
     `prestart_registry.remove_prestart`），换一个用户名再问同一句，注入块必须消失
  7. 按第 1 条抓到的那份注册恢复回去，并回读清单确认参数一模一样 —— 这条测的是
     "判据自己没把系统留在坏状态里"，因为那份注册写在挂载进去的
     faymcp/data/mcp_prestart_tools.json 里，是会跟着实例活下去的

**禁用工具掐不断这条路，run #1 实测出来的。** 原来这里写的对照是「把工具的启用状态关掉」，
理由是 `get_enabled_tools()` 与 prestart 的 runnable 清单都过滤 enabled。两头都读了源码、
两头都不成立：预启动的执行走 llm/nlp_cognitive_stream.py:457
`mcp_runtime.call_tool(..., skip_enabled_check=True)` → faymcp/mcp_service.py:400 那句
`if not skip_enabled_check and not get_tool_state(...)` 直接被跳过；而
`runtime_bridge.list_runnable_prestart_tools()` 取快照时传的是 `include_disabled=True`，
它只按 server 在不在、工具还在不在清单里筛，**根本不看 enabled**。所以关掉启用位之后
:5010 的清单仍然列着它、每一轮仍然照查不误（实测：禁用后那一行里 `<prestart>` 块还在）。
这条判据要的杠杆只有摘注册那一个，第 6(b) 条于是改成摘注册 —— 顺带说一句，这本身就是个
值得知道的事实：想临时停掉知识库注入，去管理台把工具「禁用」是没用的。

代价，讲清楚再跑：一组要发两整轮问答（正常轮 + 摘注册的对照轮），每轮都要过一遍大模型。
远端 26B 实测 12.0s 一发；本机 9b 只有 6% 权重进显存时同一句话 172~301s。所以它**不在
`run.sh test` 的常态组里**，由人用 `./run.sh kbq [问题] [期望词]` 发起，超时默认 520s
（与 INTEGRATION 那条预算链上「后端 520s」同一档）。

还有一条同样跑出来的收工时机，写在 ask_once 里：那一轮模型可能中途改走去调工具，run #1
实测正文先停在「我来帮你查一下，稍等…」，76.5s 之后才补完剩下的回答和 `<dh-end>`。所以这里
的静默兜底是 `--quiet` 的 180s，不是 adapter 那个 8s —— 拿 8s 判这一组，红的是探针的耐心。

用法：`./run.sh kbq [问题] [期望词]`，或 python kb_fay_probe.py
[--base http://fay:5000] [--ask 问题] [--want 词] [--timeout 520] [--quiet 180] [--no-control]。
`--want` 给空串时，两条「含期望词」的判据（注入块那条、正文软证据那条）记 SKIP，其余照判：
换问法时人给不出期望词，那就别硬造一个。
退出码 = 失败的判据数（0 表示没有红；SKIP 不算红，但会单独列出来）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fay_probe as fp  # noqa: E402  复用 record()/RESULTS/http_post/wait_http/skip_tail

DEFAULT_ASK = "血压的正常参考值是多少？"
# 期望词取自真实语料（seed/kb_corpus/健康监测数据.docx 经 slice_kb_corpus.py 切片后的
# 一段），它同时是 `./run.sh kb` 第 2 条 golden 的期望词 —— 两处问同一件事才看得出
# 「链路通但答不出来」这种分歧。
DEFAULT_WANT = "135/85"
# 反向对照 (a) 用的假词：语料里不存在，模型也不会自己说出这个带分隔符的串。
FAKE_WANT = "135/85·本探针造的假词"
SOURCE_MARK = "〔"          # 切片器给每条片段打的出处前缀，只有注入块里有
TOOL = "query_yueshen"
BODY_MIN = 30               # 正文少于这个字数就当没答出来（"好的"、"我来想想"之类）
# 大模型出口失败时 Fay 自己兜的那两句（nlp_cognitive_stream.py:2093 与 :2288），
# 共同点是「请稍后再试」。带上 :5010 那侧的「连接失败」做保险：那一层的话术改了也不至于漏判。
BROKEN_MARKS = ("请稍后再试", "连接失败")

PRE_RE = re.compile(r"<prestart>([\s\S]*?)</prestart>", re.IGNORECASE)
HEAD_RE = re.compile(r"【([^】]+)】\(([^)]*)\)")
QUERY_RE = re.compile(r"query=(.*?)(?:,\s*top_k|\s*\)|$)")
END_RE = re.compile(r"<dh-end>", re.IGNORECASE)
STEP_RE = re.compile(r"共\s*(\d+)\s*步")


def get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def json_post(base: str, path: str, payload: dict, timeout: float) -> dict:
    """:5010 那几个管理口读的是 `request.json`，与 /api/send 那组口的表单 `data=` 不是一套约定。"""
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def ask_once(base: str, user: str, question: str, timeout: float,
             quiet: float = 180.0) -> tuple[str, float]:
    """问一句，把 Fay 自己那一行的**原文**收回来（不去标签 —— 标签就是证据）。

    收工时机只有 `<dh-end>` 一个，静默兜底放宽到 quiet（默认 180s）而不是 8s。这不是
    调参数调出来的，是第一次跑出来的：run #1 里正文停在「我来帮你查一下，稍等…」，
    探针按 8s 静默收了工，那一行却在 76.5s 之后接着写完 —— 里面是
    「执行耗时: 76.5s，共 0 步」那段 think，然后是真正的回答和 `<dh-end>`。模型这一轮走的是
    工具规划那条路，中间那段**本来就不往库里写东西**，静默是常态不是事故。照 8s 判，
    判的是探针的耐心。
    8s 那个数是 adapter 的兜底，它那边不能干等哨兵；这里可以，因为有 --timeout。
    """
    started = time.monotonic()
    before = fp.http_post(base, "/api/get-msg", {"username": user, "limit": 5, "offset": 0})
    baseline = max([int(r["id"]) for r in before["list"]] or [0])
    fp.http_post(base, "/api/send", {"username": user, "msg": question})
    sig: dict[int, int] = {}
    rows: dict[int, str] = {}
    last_growth = started
    while time.monotonic() - started < timeout:
        time.sleep(0.8)
        fresh = {int(r["id"]): r["content"] for r in
                 fp.http_post(base, "/api/get-msg", {"username": user, "limit": 20, "offset": 0})["list"]
                 if r["type"] == "fay" and int(r["id"]) > baseline and (r["content"] or "").strip()}
        if {k: len(v) for k, v in fresh.items()} != sig:
            rows, sig, last_growth = fresh, {k: len(v) for k, v in fresh.items()}, time.monotonic()
        elif rows and (END_RE.search("".join(rows[k] for k in sorted(rows)))
                       or time.monotonic() - last_growth >= quiet):
            break
    return "".join(rows[k] for k in sorted(rows)), time.monotonic() - started


def judge_round(tag: str, blob: str, secs: float, question: str, want: str,
                expect_injection: bool) -> None:
    """按第 2~5 条判一次问答。expect_injection=False 时第 2 条反过来判（对照轮）。"""
    pre = PRE_RE.search(blob)
    head = HEAD_RE.search(pre.group(1)) if pre else None
    tool = head.group(1) if head else ""
    got_q = QUERY_RE.search(head.group(2)).group(1).strip() if head and QUERY_RE.search(head.group(2)) else ""
    body = blob[pre.end():] if pre else blob

    if expect_injection:
        same = got_q == question
        fp.record(bool(pre) and tool == "query_yueshen" and same,
                  f"{tag} 这一问真驱动了 query_yueshen（注入块的 query 逐字等于问句）",
                  f"块头 query={got_q[:60]!r}" + ("" if same else f" ≠ 问句 {question[:60]!r}"))
        injected = pre.group(1) if pre else ""
        # 第 3 条判的是**注入回来的那段原文**：它是工具输出，不经采样，所以「带出处标记」
        # 「含某个词」都是硬判据 —— 灌错语料、top_k 改坏、检索跑错集合，这里当场就红。
        fp.record(SOURCE_MARK in injected, f"{tag} 注入块里是带出处标记的语料片段（不是空块或报错）",
                  f"注入 {len(injected)} 字，含标记 {SOURCE_MARK in injected}"
                  + ("" if injected else " —— 整块都不在，第 2 条已经先红了"))
        if want:
            fp.record(want in injected, f"{tag} 注入回来的片段含「{want}」",
                      f"注入 {len(injected)} 字，含词 {want in injected}"
                      + ("" if want in injected else f"；注入块起首 {injected[:70]!r}"))
        else:
            # 换问法时人给不出期望词，那就别硬造一个：判不了就说判不了，其余各条照判。
            fp.record(None, f"{tag} 注入回来的片段含期望词",
                      "未给 --want，这条判不了（第 2、3、5、6 条与它无关）")
        # 第 4 条判的是**生成结果**，所以它只能判「这一问有没有真的答出来」，不能判用词。
        # 为什么不给「正文含 135/85」记红：run #1 里同一问、同一份注入，正文写的是
        # 「120-139/80-89…140/90」，一个 135 也没提，可它把话说完了；而语料里那句
        # 「家庭自测 ≥ 135/85」在另一次运行里被模型写成「收缩压 ≥ 135 mmHg 或舒张压 ≥ 85 mmHg」
        # —— 逐字串 "135/85" 两次都不在正文里。把复现原话当判据，判的是采样脾气，
        # 一周里总会随机红几天。所以正文只判「不是空话」，复现与否作为软证据单独报一行。
        steps = STEP_RE.search(blob)
        answered = len(body.strip()) >= BODY_MIN and not any(bad in body for bad in BROKEN_MARKS)
        fp.record(answered, f"{tag} 这一问真的答出来了（注入之后正文不是空话）",
                  f"正文 {len(body)} 字：{body.strip()[:60]!r}"
                  + (f"（think 里共 {steps.group(1)} 步，这一轮走的是工具规划那条路）" if steps else ""))
        if want:
            # 复现与否只报不判红：没复现记 None（SKIP）而不是 False，否则这条就是拿
            # 采样脾气当 CI。这一轮真正的硬判据是上面那句「答出来了」和注入块那侧的含词。
            if want in body:
                fp.record(True, f"{tag} 软证据：正文复现了知识库里的「{want}」",
                          f"正文 {len(body)} 字，含词 True")
            else:
                fp.record(None, f"{tag} 软证据：正文没复现「{want}」（不计失败，理由见第 4 条那段注释）",
                          f"正文 {len(body)} 字，模型用的是它自己的说法；正文起首 {body.strip()[:70]!r}")
        else:
            fp.record(None, f"{tag} 软证据：正文复现期望词",
                      "未给 --want，这条判不了（上面那条「答出来了」与它无关）")
        fp.record(bool(END_RE.search(blob)),
                  f"{tag} 回帧以 <dh-end> 收尾（fay/0008 哨兵契约）",
                  f"{secs:.1f}s 墙钟，注入 {len(injected)} 字，正文 {len(body)} 字"
                  + ("" if END_RE.search(blob) else
                     f" —— 等了 {secs:.0f}s 仍没等到哨兵；这一轮大概还在写，把 --timeout 放宽"))
    else:
        fp.record(not pre, f"{tag} 对照：摘掉 prestart 注册之后注入块消失",
                  (f"仍有注入块：query={got_q[:60]!r} —— 说明注入还有第二条来源，"
                   "或者摘注册没生效" if pre
                   else f"注入块不在了，正文 {len(body)} 字：{body.strip()[:70]!r}"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://fay:5000")
    ap.add_argument("--ask", default=DEFAULT_ASK)
    ap.add_argument("--want", default=DEFAULT_WANT)
    # 520s 与 INTEGRATION 预算链上「后端 520s」同一档：这一组是业务链的镜像，
    # 上游谁先放弃，这里就该在同样的位置上撞墙。
    ap.add_argument("--timeout", type=float, default=520.0)
    ap.add_argument("--quiet", type=float, default=180.0,
                    help="连续多少秒不再涨字、又没见到 <dh-end> 才收工（默认 180s：模型走"
                         "工具规划那条路时，中间那几十秒本来就不往回答里写字）")
    ap.add_argument("--no-control", action="store_true",
                    help="跳过第 6(b) 条对照（那一条会临时摘掉 query_yueshen 的 prestart 注册"
                         "再多问一轮；只想看注入在不在的时候可以不跑，但那样第 2、3 条就没有"
                         "『判据不是白给的』证据了）")
    args = ap.parse_args()

    host = urllib.parse.urlparse(args.base).hostname or "127.0.0.1"
    svc = f"http://{host}:5010"
    if not fp.wait_http(args.base, 120):
        fp.record(False, "Fay HTTP 就绪 (/api/get-system-status)", args.base)
        return len([r for r in fp.RESULTS if r[0] is False])

    # --- 1. 注入这条路是不是 prestart 注册出来的（不注册就没有那个块，后面全无从判起）---
    # 顺手把这份注册原样留着：第 7 条按它恢复。不写常量是因为常量是我猜的，而这份是实例
    # 此刻真正在用的 —— top_k 被人改成 8 之后，探针不该又把它改回 3。
    orig: dict | None = None
    try:
        runnable = get_json(svc + "/api/mcp/prestart/runnable", 30.0)
        tools = runnable.get("prestart_tools") or []
        kb = next((t for t in tools if t.get("tool") == TOOL), None)
        orig = kb
        tmpl = str((kb or {}).get("params", {}).get("query", ""))
        fp.record(kb is not None and "{{question}}" in tmpl,
                  f"1 {TOOL} 注册为 prestart 且参数模板是 {{{{question}}}}",
                  f"清单 {[(t.get('tool'), t.get('params')) for t in tools]}"
                  if kb is None else
                  f"server_id={kb.get('server_id')} params={kb.get('params')} "
                  f"include_history={kb.get('include_history')} "
                  f"allow_function_call={kb.get('allow_function_call')}")
        sid = (kb or {}).get("server_id", 4)
    except Exception as exc:
        fp.record(False, f"1 {TOOL} 注册为 prestart 且参数模板是 {{{{question}}}}", repr(exc))
        sid = 4

    stamp = int(time.time())
    blob, secs = ask_once(args.base, f"probe_kb_{stamp}", args.ask, args.timeout,
                          quiet=args.quiet)
    # --- 2~5. 主判据 ---
    judge_round("2-5", blob, secs, args.ask, args.want, expect_injection=True)

    # --- 6(a). 假词不在这行里：不然「含词」这条判据就是白给的，换什么词都算命中 ---
    fp.record(FAKE_WANT not in blob,
              "6a 反向对照：语料里不存在的假词没有「命中」",
              f"真词「{args.want}」在这行里出现 {blob.count(args.want)} 次"
              if args.want else f"未给 --want，只能报假词出现 {blob.count(FAKE_WANT)} 次")

    # --- 6(b). 把这台工具从 prestart 注册里摘掉，注入块必须消失 ---
    # 为什么不是「禁用工具」：那条掐不断这条路，实测掐不断（见 docstring 里那段
    # skip_enabled_check 的说明）。摘注册才是这一条路的开关，代价是它写的是挂载进去的
    # faymcp/data/mcp_prestart_tools.json —— 所以恢复必须放在 finally，且第 7 条要回读。
    if args.no_control:
        fp.record(None, "6b 对照：摘掉 prestart 注册之后注入块消失",
                  "--no-control 跳过，本轮没有『判据不是白给的』这条证据")
    elif orig is None:
        fp.record(False, "6b 对照：摘掉 prestart 注册之后注入块消失",
                  "第 1 条就没读到注册，对照与恢复都无从下手 —— 不去猜一份参数把它写回去")
    else:
        try:
            json_post(svc, f"/api/mcp/servers/{sid}/tools/{TOOL}/prestart",
                      {"enabled": False}, 30.0)
            cblob, csecs = ask_once(args.base, f"probe_kbc_{stamp}", args.ask, args.timeout,
                                    quiet=args.quiet)
            judge_round("6b", cblob, csecs, args.ask, args.want, expect_injection=False)
        except Exception as exc:
            fp.record(False, f"6b 对照：摘掉 {TOOL} 的 prestart 注册后注入块消失", repr(exc))
        finally:
            try:
                json_post(svc, f"/api/mcp/servers/{sid}/tools/{TOOL}/prestart",
                          {"enabled": True, "params": orig.get("params") or {},
                           "include_history": bool(orig.get("include_history", True)),
                           "allow_function_call": bool(orig.get("allow_function_call", False))},
                          30.0)
                back = next((t for t in get_json(svc + "/api/mcp/prestart/runnable", 30.0)
                             .get("prestart_tools") or [] if t.get("tool") == TOOL), None)
                same = back is not None and (back.get("params") or {}) == (orig.get("params") or {})
                fp.record(same, f"7 恢复 {TOOL} 的原始 prestart 注册并回读确认",
                          f"参数回到 {orig.get('params')}"
                          if same else f"回读到 {back and back.get('params')}，与原来的 "
                                       f"{orig.get('params')} 不一致 —— 得人去管理台确认")
            except Exception as exc:
                fp.record(False, f"7 恢复 {TOOL} 的原始 prestart 注册并回读确认",
                          f"{exc!r}｜知识库注入现在大概率是停的，手工恢复："
                          f" curl -X POST {svc}/api/mcp/servers/{sid}/tools/{TOOL}/prestart "
                          f"-H 'Content-Type: application/json' -d '"
                          + json.dumps({"enabled": True, "params": orig.get("params") or {},
                                        "include_history": bool(orig.get("include_history", True)),
                                        "allow_function_call": bool(
                                            orig.get("allow_function_call", False))},
                                       ensure_ascii=False) + "'")

    failed = [r for r in fp.RESULTS if r[0] is False]
    skipped = [r for r in fp.RESULTS if r[0] is None]
    passed = [r for r in fp.RESULTS if r[0] is True]
    print(f"\n[kb-fay] 通过 {len(passed)}/{len(fp.RESULTS)}，跳过 {len(skipped)}"
          f"（问「{args.ask}」；这一组不在 ./run.sh test 常态清单里）", flush=True)
    for _, name, detail in skipped:
        print(f"  - {name}  {detail}", flush=True)
    for _, name, detail in failed:
        print(f"  ✗ {name}  {detail}", flush=True)
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
