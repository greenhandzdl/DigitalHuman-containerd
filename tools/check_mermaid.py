"""mermaid 块的本地结构自查：不依赖任何渲染器（外部渲染器一旦开始回 000/500，
就分不清是语法坏了还是它自己不舒服 —— 这种检查必须能在离线时给出结论）。

查的是最容易把解析器搞坏的几类：引号/方括号/圆括号成对、`subgraph`/`loop` 与 `end` 数量对齐、
节点标签里的 `(` 必须待在引号里面（`[( … )]` 那种圆柱体节点除外，那是形状语法不是标签）、
每条边的 `|...|` 成对、首行是合法的图类型。
"""
import re
import sys
from pathlib import Path

PAIRS = {")": "(", "]": "[", "}": "{"}
BLOCK_OPEN = re.compile(r"^\s*(subgraph|loop|alt|opt|par|critical|rect|box)\b")
# sequenceDiagram 里这几类行没有"给标签加引号"这回事：`participant B as 浏览器（…）` 的
# 别名部分不吃引号，加了反而会原样显示。所以括号那条只适用于 flowchart 式的节点/边语句。
NO_QUOTE_SYNTAX = re.compile(
    r"^\s*(participant|actor|note|loop|alt|opt|par|critical|rect|end|activate|deactivate|"
    r"title|caption|linkStyle|style|class)\b")
SHAPES = [  # 形状语法先换掉，剩下的括号才是"标签里的括号"
    (re.compile(r"\[\((\"[^\"]*\")\)\]"), r"[$1]"),
    (re.compile(r"\[\[\"([^\"]*)\"\]\]"), r"[\1]"),
    (re.compile(r"\(\(\"([^\"]*)\"\)\)"), r"[\1]"),
    (re.compile(r"\[\"([^\"]*)\"\]"), r"[\1]"),
    (re.compile(r"\(\(([^\"]*)\)\)"), r"[\1]"),
]

bad = 0
for path in sys.argv[1:]:
    text = Path(path).read_text(encoding="utf-8")
    for bi, block in enumerate(re.findall(r"```mermaid\n(.*?)```", text, re.S), 1):
        errs = []
        stack = []
        for line_no, raw in enumerate(block.split("\n"), 1):
            line = raw.replace('\\"', "\x01")          # 转义引号先藏起来，别当引号配对
            s = line.strip()
            if not s or s.startswith("%%"):
                continue
            if s.count('"') % 2:
                errs.append(f"{line_no}: 引号不成对 {s[:60]!r}")
            bare = re.sub(r'"[^"]*"', '""', s)         # 引号内的内容不参与括号统计
            if re.search(r'\|"[^"]*"', s) and len(re.findall(r"\|", s)) != 2:
                errs.append(f"{line_no}: 边标签的 | 不是两条 {s[:60]!r}")
            for rx, rep in SHAPES:
                bare = rx.sub(rep, bare)
            for ch in bare:
                if ch in "([{":
                    stack.append((ch, line_no))
                elif ch in PAIRS:
                    if not stack or stack[-1][0] != PAIRS[ch]:
                        errs.append(f"{line_no}: {ch} 没有对应的开括号 {s[:60]!r}")
                    else:
                        stack.pop()
            if re.search(r"[（(]", bare) and not NO_QUOTE_SYNTAX.match(s):
                # 半角括号会直接被解析器吃掉；全角那对虽然通常能过，但本仓库的规矩是
                # "标签一律加引号"，所以这里一起拦 —— 少一类要靠人记的例外。
                errs.append(f"{line_no}: 括号裸露在未加引号的标签里 {s[:60]!r}")
        if stack:
            errs.append("未闭合的开括号：" + ", ".join("%s@%d" % (c, l) for c, l in stack))
        n_open = len([l for l in block.split("\n") if BLOCK_OPEN.match(l)])
        n_end = len(re.findall(r"^\s*end\s*$", block, re.M))
        if n_open != n_end:
            errs.append(f"块开始（subgraph/loop/…）{n_open} 个 / end {n_end} 个，不匹配")
        head = block.split("\n", 1)[0].strip()
        if not re.match(r"^(flowchart|sequenceDiagram|graph|classDiagram|stateDiagram)\b", head):
            errs.append(f"首行不是合法的图类型：{head!r}")
        print(f"{'OK ' if not errs else 'BAD'} {path} block#{bi}"
              f"（{len(block.splitlines())} 行，块开始 {n_open}，{head.split()[0]}）")
        for e in errs:
            bad += 1
            print(f"      ✗ {e}")
print(f"结构自查不通过项：{bad}")
sys.exit(1 if bad else 0)
