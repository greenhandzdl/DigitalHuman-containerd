"""科普语料入库 + 检索抽测（`./run.sh kb`）。

和 `probe-yueshen` 的分工：那一组验的是「知识库这条**链路**通不通」（MCP 面、
嵌入出口、五步链路、条数对账），用的是镜像里那份 3 段的合成语料；这一组验的是
「项目方给的**那批数据**到底进了库、问得出来」——切片的成果全部体现在这里。
两边都走 Fay :5010 的 MCP 管理面调 yueshen 的三个工具，不自己另接一条 SSE。

为什么必须真问一遍而不是只看 ingest 返回的 inserted：上游 `upsert_chunks` 对
embedding 失败是「跳过这条 chunk」而不抛错（server.py:363），inserted 对得上只
说明写进去了，说明不了检索回得来。

下面 12 问是从切片内容里挑的，每问的期望词都必须出现在**该问该去的那份文件**里
（见 --list 打印的出处）。它们同时是切片的验收：合成语料那种「段落里有个标记词」
的判据测不出表格摊平得好不好，只有拿真实问法去问才测得出。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

# (问题, 期望命中的词, 该条内容出自哪份语料)
GOLDENS: list[tuple[str, str, str]] = [
    ("老人摔倒了能不能马上自己站起来？", "跌倒", "应急指引"),
    ("血压的正常参考值是多少？", "收缩压", "健康监测数据"),
    ("药漏服了要不要一次补两倍？", "漏服", "用药护理"),
    ("踝泵运动怎么做？", "踝泵", "通用康复动作库"),
    ("训练时觉得头晕心慌还要继续吗？", "头晕", "训练动作答疑数据集"),
    ("出院以后居家康复要分几个阶段？", "阶段", "出院居家康复计划通用模板"),
    ("人上了年纪肌肉为什么会变少？", "肌肉", "老年康养通用科普"),
    ("老人情绪低落，家属该怎么陪伴？", "陪伴", "情绪安抚与陪伴话术"),
    ("慢阻肺的呼吸训练每分钟做几次？", "呼吸循环", "训练动作答疑数据集"),
    ("康复训练有哪些禁忌情况？", "禁忌", "康复安全与禁忌"),
    ("出院后多久要回医院复查？", "复查", "居家康复复查随访标准数据"),
    ("高血压老人做力量训练要注意什么？", "高血压", "康复合并慢病问答数据"),
]


def get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def tool_text(out: dict) -> str:
    return "".join(c.get("text", "") for c in ((out.get("result") or {}).get("content") or [])
                   if isinstance(c, dict))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://fay:5000")
    # 900s 是「516 个片段全量重嵌入」的上限，不是随手取的：本机 qwen3-embedding:0.6b
    # 换入就要 70s 起，探针那 240s 只够三份合成语料。
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--no-reset", action="store_true",
                    help="追加而不是清空重建（默认 reset，和探针同一套语义）")
    ap.add_argument("--list", action="store_true", help="只打印 12 问与出处，不碰服务")
    args = ap.parse_args()

    if args.list:
        for q, want, src in GOLDENS:
            print(f"{q}  →  须含「{want}」，出自 {src}")
        return 0

    host = urllib.parse.urlparse(args.base).hostname or "127.0.0.1"
    svc = f"http://{host}:5010"

    raw = get_json(svc + "/api/mcp/servers", 30.0)
    # 这个管理面在有的版本上返回 {"servers": [...]}、有的直接返回列表，与 yueshen_probe 同一取法。
    servers = raw.get("servers") if isinstance(raw, dict) else raw
    kb = next((s for s in servers or [] if "yueshen" in str(s.get("name", "")).lower()), None)
    if kb is None:
        print(f"[kb] 清单里没有 yueshen 这一台：{[s.get('name') for s in servers]}", file=sys.stderr)
        return 1
    sid = kb["id"]
    connected_here = kb.get("status") != "online"
    if connected_here:
        out = post_json(svc + f"/api/mcp/servers/{sid}/connect", {}, max(args.timeout, 90.0))
        if not out.get("success"):
            print(f"[kb] 连不上 yueshen：{out.get('message')}", file=sys.stderr)
            return 1

    def call(name: str, arguments: dict) -> dict:
        # Fay 那份 /call 的入参形状与 MCP 不一样：method 直接就是工具名、params 就是
        # 工具的 arguments（不是 MCP 的 {"name":…,"arguments":…}）—— 与 yueshen_probe
        # 的 call_tool 同一取法。写错的话服务端回 "unknown tool: tools/call"。
        out = post_json(svc + f"/api/mcp/servers/{sid}/call",
                        {"method": name, "params": arguments, "is_prestart": True}, args.timeout)
        if not out.get("success"):
            raise RuntimeError(f"{name} 调用失败: {str(out)[:200]}")
        got = json.loads(tool_text(out))
        # 工具自己的失败藏在 content 里那份 JSON 的 success 字段：Fay 那一层是成功的
        # （HTTP 200、success:true），所以只看外层会把 "unknown tool" 当成一次空入库。
        if isinstance(got, dict) and got.get("success") is False:
            raise RuntimeError(f"{name} 工具报错: {str(got.get('message'))[:200]}")
        return got

    try:
        ing = call("ingest_yueshen", {"reset": not args.no_reset})
        chunks, inserted = int(ing.get("chunks") or 0), int(ing.get("inserted") or 0)
        print(f"[kb] 入库 chunks={chunks} inserted={inserted}｜语料 {ing.get('corpus_dir')}"
              f"｜嵌入 {ing.get('embedding_model')}")
        if chunks <= 0 or inserted != chunks:
            print(f"[kb] 入库就没成功，后面的检索不测了。返回：{json.dumps(ing, ensure_ascii=False)[:400]}",
                  file=sys.stderr)
            return 1

        st = call("yueshen_stats", {})
        vectors = st.get("vectors")
        print(f"[kb] 向量库 vectors={vectors} 集合={st.get('collection')} 持久化={st.get('persist_dir')}")

        miss = 0
        for q, want, src in GOLDENS:
            res = call("query_yueshen", {"query": q, "top_k": args.top_k})
            hits = [(r or {}).get("document") or "" for r in (res.get("results") or [])]
            blob = "\n".join(hits)
            hit = want in blob
            miss += 0 if hit else 1
            print(f"  {'OK  ' if hit else 'MISS'} 「{q}」 top{args.top_k}={res.get('count')}"
                  f" 期望「{want}」出自 {src}"
                  + ("" if hit else f"｜回来的前 90 字：{blob[:90]!r}"))
    finally:
        if connected_here:
            try:
                post_json(svc + f"/api/mcp/servers/{sid}/disconnect", {}, 30.0)
            except Exception as exc:
                print(f"  ! 断开 server_id={sid} 失败（一次性容器退出后由 Fay 的连接检查兜底）: {exc!r}")

    print(f"\n[kb] 检索抽测 {len(GOLDENS) - miss}/{len(GOLDENS)} 命中"
          + ("" if miss == 0 else f"，{miss} 问未命中"))
    return 0 if miss == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
