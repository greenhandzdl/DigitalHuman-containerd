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

判据不止「命中/未命中」两档，因为改 top_k、改切片粒度都要有表可看，不能靠手感：

  * 每条命中都打 `source·page@distance`，退化成「top-3 里混进无关片段」时看得见；
    距离口径注意 —— 上游建集合时没传 `hnsw:space`（`fay/mcp_servers/yueshen_rag/
    server.py:322,:326`），Chroma 默认给的是 **L2 平方**，越小越近，而且服务端
    不做任何阈值过滤（:366-376），所以「弱命中就别注入」这件事现在根本不存在。
  * `--sweep 3,5,8` 对同一批向量逐档重查，打印 recall@k 矩阵。
  * 最后一行是**反向对照**：把某条 golden 的期望词换成一个语料里不存在的串，
    这条必须变红。它测的不是知识库而是判据本身 —— 换个假词照样"命中"，
    那 12/12 就是判据自己让出来的（典型是 tool 没按 top_k 截、把整库都回了）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

# (问题, 期望命中的词, 该条内容出自哪份语料)
# 第三列是**出处说明**，不是断言：判据只查第二列那个词在不在回来的 top-k 里。
# 实测有对不上的：「训练时觉得头晕心慌还要继续吗？」的 top-8 全部来自
# 通用动作异常判定参考数据.docx 与 应急指引.docx，一条 训练动作答疑数据集 都没有 ——
# 「头晕」这个词在别的文件里也有。词命中是真的，出处只是给人看的线索。
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

# 反向对照：锚定第 2 条 golden。NEG_TERM 是探针自己造的词，语料里不存在，
# 它「命中」就说明回来的文本不受 top_k 约束（或者 blob 拼错了东西）。
NEG_Q = "血压的正常参考值是多少？"
NEG_WANT = "收缩压"
NEG_TERM = "收缩压·本探针造的假词"


def run_goldens(call, top_k: int, goldens=GOLDENS, verbose: bool = True) -> list[dict]:
    """逐问检索，返回每条 golden 的判定明细（hit / count / text）。

    明细里把 top_k 每条的出处和距离都打出来：只报「命中」的话，`top_k` 从 3 提到 8
    这类改动到底是把对的挤掉了还是真多捞回一条，看不出来。
    """
    rows = []
    for q, want, src in goldens:
        res = call("query_yueshen", {"query": q, "top_k": top_k})
        results = res.get("results") or []
        hits, parts = [], []
        for r in results:
            r = r or {}
            meta = r.get("metadata") or {}
            doc = r.get("document") or ""
            parts.append(doc)
            where = os.path.basename(str(meta.get("source") or "?"))
            if meta.get("page") is not None:
                where += f"·p{meta['page']}"
            dist = r.get("distance")
            if dist is not None:
                try:
                    where += f"@{float(dist):.3f}"
                except (TypeError, ValueError):
                    pass
            hits.append(where)
        text = "\n".join(parts)
        hit = want in text
        rows.append({"q": q, "want": want, "src": src, "hit": hit,
                     "count": int(res.get("count") or 0), "text": text})
        if verbose:
            print(f"  {'OK  ' if hit else 'MISS'} 「{q}」 top{top_k}={rows[-1]['count']}"
                  f" 期望「{want}」出自 {src}")
            print(f"       回来的：{'、'.join(hits) or '（空库，一条都没有）'}"
                  + ("" if hit else f"｜前 90 字：{text[:90]!r}"))
    return rows


def negative_control(rows: list[dict], top_k: int) -> bool:
    """假词必须不命中、回来的条数必须不越过 top_k —— 否则 12/12 不算证据。"""
    src_code = open(os.path.abspath(__file__), encoding="utf-8").read()
    anchor = f'("{NEG_Q}", "{NEG_WANT}", "健康监测数据"),'
    if anchor not in src_code:
        print("FAIL  反向对照：GOLDENS 里找不到这一问的锚点，锚定已经和这份自检漂走了")
        return False
    if any(NEG_TERM in want for _q, want, _s in GOLDENS):
        print("FAIL  反向对照：假词竟然是一条 golden 的期望词，对照失效")
        return False
    row = next((r for r in rows if r["q"] == NEG_Q), None)
    if row is None or NEG_WANT not in row["text"]:
        print(f"FAIL  反向对照：这一问本轮就没命中「{NEG_WANT}」，先修主判据再谈对照")
        return False
    if row["count"] > top_k:
        print(f"FAIL  反向对照：要了 top{top_k} 却回来 {row['count']} 条，检索没截断")
        return False
    if NEG_TERM in row["text"]:
        print(f"FAIL  反向对照：语料里没有的词也「命中」了 —— 回来的不是 top{top_k} 而是整库")
        return False
    print(f"PASS  反向对照：锚点仍在 GOLDENS 里、把「{NEG_WANT}」换成假词就不成立，"
          f"且 top{top_k} 截断有效（这一问回来 {row['count']} 条）")
    return True


def print_sweep(table: dict[int, list[dict]], ks: list[int]) -> None:
    """recall@k 矩阵：行是问题，列是各 k 下能否捞回期望词。

    要的是「哪一问要第几名才够」，所以逐问打；只有总数的那张表定不了 top_k。
    """
    head = "  ".join(f"k={k}" for k in ks)
    print(f"\n[kb] recall@k（{len(GOLDENS)} 问）  {head}")
    for q, want, _src in GOLDENS:
        marks = "  ".join("✔ " if next(r for r in table[k] if r["q"] == q)["hit"] else "✗ "
                          for k in ks)
        first = next((k for k in ks if next(r for r in table[k] if r["q"] == q)["hit"]), None)
        print(f"  {marks}  「{q}」"
              + (f"  ← 要 k≥{first} 才捞得回" if first else "  ← 这些 k 里都没有"))
    print("  " + "  ".join(f"{sum(1 for r in table[k] if r['hit']):>2}/{len(GOLDENS)}" for k in ks)
          + "  ← recall@k")


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
    ap.add_argument("--sweep", metavar="K1,K2,…", default="",
                    help="同一批向量逐档重查，打印 recall@k 矩阵（诊断用，例如 --sweep 3,5,8）")
    ap.add_argument("--no-reset", action="store_true",
                    help="追加而不是清空重建（默认 reset，和探针同一套语义）")
    ap.add_argument("--list", action="store_true", help="只打印 12 问与出处，不碰服务")
    args = ap.parse_args()

    if args.list:
        for q, want, src in GOLDENS:
            print(f"{q}  →  须含「{want}」，出自 {src}")
        return 0

    # 有序去重：print_sweep 和「以最大档为准出结论」都按这个列表走
    ks: list[int] = []
    if args.sweep:
        try:
            ks = sorted({int(x) for x in args.sweep.replace("，", ",").split(",") if x.strip()})
        except ValueError:
            ap.error("--sweep 要的是逗号分隔的整数，例如 --sweep 3,5,8")
        if len(ks) < 2:
            ap.error("--sweep 至少两档才有对照意义（单档直接用 --top-k）")

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

        if ks:
            # 一次入库、多档查询：向量没变，改的只是 n_results，所以各档之间可比。
            table = {k: run_goldens(call, k, verbose=(k == max(ks))) for k in ks}
            print_sweep(table, ks)
            miss = len(GOLDENS) - sum(1 for r in table[max(ks)] if r["hit"])
            judged = f"最大档 k={max(ks)}"
            neg_ok = negative_control(table[max(ks)], max(ks))
        else:
            rows = run_goldens(call, args.top_k)
            miss = sum(1 for r in rows if not r["hit"])
            judged = f"top{args.top_k}"
            neg_ok = negative_control(rows, args.top_k)
    finally:
        if connected_here:
            try:
                post_json(svc + f"/api/mcp/servers/{sid}/disconnect", {}, 30.0)
            except Exception as exc:
                print(f"  ! 断开 server_id={sid} 失败（一次性容器退出后由 Fay 的连接检查兜底）: {exc!r}")

    print(f"\n[kb] 检索抽测 {len(GOLDENS) - miss}/{len(GOLDENS)} 命中（{judged}）"
          + ("" if miss == 0 else f"，{miss} 问未命中"))
    return 0 if (miss == 0 and neg_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
