#!/usr/bin/env python3
"""yueshen 知识库的端到端判据：Fay 管理面 → MCP/SSE → chromadb → 宿主 ollama 嵌入。

为什么单开一组而不是塞进 fay_probe 的问答轮次里：这条判据要调 ollama 的**嵌入**模型
（qwen3-embedding:0.6b），而这台机器显存只够放一个模型，换入换出实测 70s 起
（README「一次问答为什么这么慢」）。放在问答轮次中间，就会把三个实例的问答全挤成
分钟级甚至超时 —— 那是我们自己制造的显存争用，不是被测对象的故障。
所以它排在 DEFAULT_GROUPS 最后：三组问答的证据已经全部落袋。

它验的是五步，缺一步都判红：
  1. 被测实例的 MCP 清单里那台 yueshen 是 sse transport 且指向本栈容器
     （overlay/<实例>/mcp_servers.json 挂上去的那一处改动；不碰模型，所以排最先）
  2. Fay 真能握手连上（patches/yueshen_rag/0001 开出来的那条 SSE 分支活着）
  3. 语料真被解析+切块（ingest 返回 chunks>0；仓库里原本一份 pdf/docx 都没有）
  4. 向量真写进去了（inserted == chunks，且 stats 的向量化条数对得上）
  5. 检索回来的确实是那一块（标记词 + 只存在于那一段的事实「48 小时」同时命中）

端到端只跑 `fay` 这一台，另外两台的 overlay 挂载不是没人管：yueshen 从
`fay_probe.HEADLESS_UNAVAILABLE` 摘掉之后，三组问答探针各自的「现场连接离线服务器」
都会真去和它握手（三台实例都已 `depends_on: yueshen-rag`，`run.sh test` 开头那条
`up -d --wait` 也确实把三台都起着），挂错就是那一组判红。这里不再去读那两台的清单：
同一份证据记三遍，只会把「这一组有几条判据」这个数弄浑。

第 4、5 步是这条判据存在的全部理由。上游 `upsert_chunks` 对 embedding 失败是
`except: 跳过这条 chunk`（server.py:363），于是 ollama 不可达时 ingest 照样返回
`success: true`，只是 `inserted: 0` —— 只看「调用成功没有」会假绿。

预检（量嵌入出口）不是为了多一条判据，是为了把「这台机器的显存不够」和
「这条链路坏了」分开：三台 Fay 实例的问答判据已经跑完，这里若因为 ollama 换入
超时判红，红的是环境不是容器。判据自己不给后面的步骤找借口 —— 只有真量到
嵌入出口不通，才把它记成「按环境降级」的 SKIP 并就地收工（第 1 步不碰模型，照判），
否则一律硬判。收工一定要留一行 tally：实测 2026-09-21 run #32，宿主显存被栈外进程占走、
9b 落到 CPU，这一组打了两条就退出，退出码 0 —— 看日志的人只会以为它跑完了六条。

用法：python yueshen_probe.py [--base http://fay:5000] [--timeout 240]
退出码 = 失败的检查项数（0 表示全绿）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fay_probe as fp  # noqa: E402  复用 record()/RESULTS/skip_tail/收尾格式

# 与 containerd/tools/make_yueshen_corpus.py 里那份语料一一对应。两份各写一遍、
# 不共享常量是故意的：改了语料忘了改探针，这条判据直接判红（检索不到标记词），
# 而不是悄悄绿过去。
MARKER = "悦肾探针语料YS20260417"
FACT = "48 小时"
QUERY = "编号里的专项随访要求里，血压与体重要在多长时间内录回系统？"


def get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def tool_text(out: dict) -> str:
    """把 /call 返回里的 content[].text 拼起来 —— 与 fay_probe.call_tool 同一个取法。"""
    return "".join(c.get("text", "") for c in ((out.get("result") or {}).get("content") or [])
                   if isinstance(c, dict))


def check_embedding_source(timeout: float) -> bool:
    """量一次「这一组判据真正依赖的那个嵌入出口」，顺带把模型换进显存。

    配置读的是被测实例那份 system.conf（与被测容器同一套 config_util + 同一份
    overlay 挂载），不是探针自己另取的一个地址 —— 否则量到的可能是别的模型。
    返回 True = 出口通，后面的步骤一律硬判；False = 已按环境记 SKIP，调用方应收工。
    """
    name = "知识库嵌入出口可用（ollama /v1/embeddings，直连不经 Fay）"
    sys.path.insert(0, os.getcwd())
    try:
        from utils import config_util as cfg

        cfg.load_config()
        base, model = cfg.embedding_api_base_url, cfg.embedding_api_model
    except Exception as exc:
        fp.record(False, name, f"读取 system.conf 失败: {exc!r}")
        return False
    started = time.monotonic()
    try:
        data = post_json(base.rstrip("/") + "/embeddings",
                         {"input": "探针：知识库链路预热。", "model": model}, timeout)
        dim = len(data["data"][0]["embedding"])
    except Exception as exc:
        elapsed = time.monotonic() - started
        # timeout 与「地址根本连不上」要分开说：前者是这台机器的显存在换模型，
        # 后者是配置或网络坏了 —— 只有前者才配得上 SKIP。
        timed_out = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
        mark = f"｜{fp.ENV_SKIP_MARK}：换入嵌入模型超过 {timeout:.0f}s" if timed_out else ""
        fp.record(None if timed_out else False, name,
                  f"{base} model={model} 本次 {elapsed:.1f}s 失败 {exc!r}{mark}")
        return False
    fp.record(True, name, f"{dim} 维，用时 {time.monotonic() - started:.1f}s"
                          f"（{model}；这一步同时把模型换进显存，下面的入库才不会撞冷启动）")
    return True


def check_overlay(host: str, timeout: float):
    """读被测实例 :5010 的清单：验 overlay 挂载生效，并取出那台 yueshen 的配置。

    三台实例各挂了一份 overlay/<实例>/mcp_servers.json（id=4 换成 sse + 本栈地址），
    这里只验被测那一台 —— 另两台的挂载由它们各自那组 `fay_probe` 覆盖，理由见模块
    docstring。这条不碰模型，所以排在预检之前：显存不够把后面降成 SKIP 时，
    它照样留得下证据。
    """
    name = f"{host} 的 yueshen 配置为 SSE 且指向本栈容器"
    try:
        raw = get_json(f"http://{host}:5010/api/mcp/servers", timeout)
    except Exception as exc:
        fp.record(False, name, repr(exc))
        return None
    servers = raw.get("servers") if isinstance(raw, dict) else raw
    kb = next((s for s in servers or [] if "yueshen" in str(s.get("name", "")).lower()), None)
    if kb is None:
        fp.record(False, name, f"清单里没有 yueshen 这一台：{[s.get('name') for s in servers or []]}")
        return None
    transport, ip = str(kb.get("transport")), str(kb.get("ip"))
    fp.record(transport == "sse" and "yueshen-rag" in ip, name,
              f"id={kb.get('id')} transport={transport} ip={ip}（overlay 挂载生效）")
    return kb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://fay:5000")
    # 240s 是「等得到 ollama 换入嵌入模型」的上限，不是随手取的：本机换入实测 70s 起，
    # 而这个数只管预热那一发。真不可达时这一组最多烧 4 分钟。
    ap.add_argument("--timeout", type=float, default=240.0)
    args = ap.parse_args()

    def fail_count() -> int:
        return len([r for r in fp.RESULTS if r[0] is False])

    host = urllib.parse.urlparse(args.base).hostname or "127.0.0.1"
    svc = f"http://{host}:5010"

    # --- 1. 清单：那台 yueshen 必须是 sse 且指向本栈（不碰模型，最先判）---
    kb = check_overlay(host, args.timeout)

    if not check_embedding_source(args.timeout):
        print(f"\n[probe] {len(fp.RESULTS)} 条已判（其中按环境降级 1 条），"
              "后面 4 条（工具清单/入库/检索/stats）没有嵌入出口就测不了，本组就地收工",
              flush=True)
        return fail_count()

    if kb is None:
        # 没这台就无从连起；上面已经把它判成 FAIL 了，这里只收尾。
        return fail_count()
    sid = kb["id"]

    # --- 2. 现场连上。autostart 是人在 UI 上的开关，测试不依赖它的状态（同 fay_probe）---
    connected_here = kb.get("status") != "online"
    n_tools = 0
    if connected_here:
        try:
            out = post_json(svc + f"/api/mcp/servers/{sid}/connect", {}, max(args.timeout, 90.0))
        except Exception as exc:
            fp.record(False, "Fay 连上 yueshen (MCP/SSE)", repr(exc))
            return fail_count()
        if not out.get("success"):
            fp.record(False, "Fay 连上 yueshen (MCP/SSE)", str(out.get("message"))[:200])
            return fail_count()
        n_tools = len(out.get("tools") or [])
    want = {"ingest_yueshen", "query_yueshen", "yueshen_stats"}
    try:
        names = {t.get("name") for t in (get_json(svc + f"/api/mcp/servers/{sid}/tools",
                                                  args.timeout) or {}).get("tools", [])}
    except Exception as exc:
        names = set()
        fp.record(False, "yueshen 工具清单", repr(exc))
    if names:
        fp.record(want <= names, "yueshen 工具清单",
                  f"{sorted(names)}（{('连上后取到 ' + str(n_tools)) if connected_here else '开机自连'}）")

    def call_tool(method: str, params: dict) -> tuple[bool, str, dict]:
        try:
            out = post_json(svc + f"/api/mcp/servers/{sid}/call",
                            {"method": method, "params": params, "is_prestart": True}, args.timeout)
        except Exception as exc:
            return False, "", {"error": repr(exc)}
        return bool(out.get("success")), tool_text(out), out

    inserted = None
    try:
        # --- 3+4. 真入库：reset 后灌，要求 chunks 与 inserted 相等 ---
        started = time.monotonic()
        ok, text, out = call_tool("ingest_yueshen", {"reset": True})
        secs = time.monotonic() - started
        try:
            ing = json.loads(text)
        except Exception:
            ing = {}
        if not ok or "inserted" not in ing:
            fp.record(False, "yueshen 知识库入库 (ingest_yueshen)",
                      (str(ing.get("message")) if ing else (text or str(out)))[:200])
        else:
            chunks = int(ing.get("chunks") or 0)
            inserted = int(ing.get("inserted") or 0)
            fp.record(chunks > 0 and inserted == chunks, "yueshen 知识库入库 (ingest_yueshen)",
                      f"chunks={chunks} inserted={inserted} 用时 {secs:.1f}s；语料 {ing.get('corpus_dir')}"
                      f"，嵌入 {ing.get('embedding_base_url')} / {ing.get('embedding_model')}"
                      + ("｜inserted<chunks == 上游吞掉了 embedding 异常（server.py:363）"
                         if chunks and inserted != chunks else ""))

        # --- 5. 真检索：回来的必须同时含标记词和只存在于那一段的事实 ---
        ok, text, out = call_tool("query_yueshen", {"query": QUERY, "top_k": 3})
        try:
            res = json.loads(text)
        except Exception:
            res = {}
        docs = [str((r or {}).get("document") or "") for r in (res.get("results") or [])]
        blob = "\n".join(docs)
        if not ok or "results" not in res:
            fp.record(False, "yueshen 知识库检索 (query_yueshen)",
                      (str(res.get("reason")) if res else (text or str(out)))[:200])
        elif res.get("skipped"):
            fp.record(False, "yueshen 知识库检索 (query_yueshen)",
                      f"工具自己说没查：{res.get('reason')!r}")
        else:
            hit = MARKER in blob and FACT in blob
            fp.record(hit, "yueshen 知识库检索 (query_yueshen)",
                      f"{res.get('count')} 条命中，问「{QUERY}」答回 {len(docs[0]) if docs else 0} 字：{blob[:70]!r}"
                      if hit else
                      f"{res.get('count')} 条命中但不含标记：{blob[:90]!r}"
                      f"（查不到 {MARKER!r} 或 {FACT!r}）")

        # --- 4 的另一半：库里真有条数，且与 ingest 对得上 ---
        ok, text, out = call_tool("yueshen_stats", {})
        try:
            st = json.loads(text)
        except Exception:
            st = {}
        vectors = st.get("vectors")
        fp.record(ok and isinstance(vectors, int) and inserted is not None and vectors == inserted,
                  "yueshen 向量库条数与入库一致 (yueshen_stats)",
                  f"vectors={vectors} 对 ingest inserted={inserted}；持久化 {st.get('persist_dir')}"
                  f"，集合 {st.get('collection')}")
    finally:
        # 探针连上的就探针断开，恢复原样：autostart=false 的实例不该被测试留在在线态
        if connected_here:
            try:
                post_json(svc + f"/api/mcp/servers/{sid}/disconnect", {}, 30.0)
            except Exception as exc:
                print(f"  ! 断开 server_id={sid} 失败（一次性探针容器退出后由 Fay 的连接检查兜底）: {exc!r}",
                      flush=True)

    failed = [r for r in fp.RESULTS if r[0] is False]
    skipped = [r for r in fp.RESULTS if r[0] is None]
    print(f"\n[probe] {len(fp.RESULTS) - len(failed) - len(skipped)}/{len(fp.RESULTS)} 通过"
          + fp.skip_tail(skipped), flush=True)
    for _, name, detail in failed:
        print(f"  ✗ {name}  {detail}", flush=True)
    for _, name, detail in skipped:
        print(f"  ○ {name}  {detail}", flush=True)
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
