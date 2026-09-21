"""把项目方给的一包 .docx 切成「每片自己说得清自己是啥」的小 .docx，喂给 yueshen-rag。

用法（必须跑在装了 python-docx 的容器里，宿主 python 没有这个包）：

    docker run --rm -v <源目录或zip>:/src:ro -v $PWD/seed/kb_corpus:/out \\
        -v $PWD/tools:/tools:ro dh-yueshen-rag:local python /tools/slice_kb_corpus.py /src /out

为什么要在仓库里再切一遍，而不是把原始 .docx 直接丢进语料目录：
yueshen_rag 的 `CorpusLoader._file_to_chunks()`（server.py:198）对 .docx 走的是
`_extract_docx()`（server.py:142），它先把**全部段落**拼成一段文本、再把**全部表格**
按 `单元格 | 单元格 | …` 追加在后面 —— 两件事因此丢了：

1. 段落与表格的先后顺序没了。表格前面那句「下表给出 36 条科普条目的分类与来源」
   和表格本体落不进同一个切块，检索回来的是一串没有主语的分隔符行。
2. 表头没了。`| K1-01 | 老年人生理特点 | 肌肉与力量 | … |` 这种行，一旦切块窗口
   （chunk_size=600, overlap=120）跨过表头，后面的行就只剩值没有列名，模型读不懂。

这个项目方语料恰好全是表格型数据集（14 份里 9 份的主内容是表格：动作库 74 张表、
科普条目 37 行、分级规则 15 行…），所以第 2 条是主要矛盾，不是可选优化。

本工具的做法：按文档真实顺序遍历块，表格**逐行摊平成「列名：值；列名：值」**，
每行前面再钉一个 `〔原文件名·当时那级标题〕` 的短前缀。长度仍压在 --max-chars
（默认 900）—— 但**每个来源只产出一个 .docx**：python-docx 空文档自带 35KB 的默认
样式表，516 片各写一文件就是 21MB，纯模板开销。前缀是逐行钉的，所以 loader 那 600
字窗口从中间切开，切开的每一段也还带着「这是哪份文件的哪一节」。

产物落在 `containerd/seed/kb_corpus/`，compose 把它只读挂进 `/app/corpus/kb`，
`ingest_yueshen` 与探针那份验证语料一起入库 —— 见 README「科普语料」。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

MANIFEST = ".slice-manifest.json"
# 只认这三种标题样式名：中文 Word 的样式名跟着客户端语言走，但项目方这 14 份是英文
# 样式名（Heading 1..3）导出的，先按实测来，别提前兼容没有证据的写法。
HEADING_STYLES = {"heading 1": 1, "heading 2": 2, "heading 3": 3, "title": 1}
MIN_CHARS = 24  # 比这还短的片段全是页眉/占位/空标题，入库只会稀释检索


def _iter_blocks(doc: Document):
    """按 body 里的真实顺序产出段落与表格 —— python-docx 的 doc.paragraphs /
    doc.tables 是两个各看一半的视图，分开取就把顺序丢了。"""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _heading_level(par: Paragraph) -> int | None:
    try:
        name = (par.style.name or "").strip().lower()
    except Exception:
        return None
    return HEADING_STYLES.get(name)


def _table_records(tbl: Table) -> list[str]:
    """每张表 → 每行一条「列名：值；列名：值」。

    表头取第一行；整表只有一行时不猜表头，原样按竖线拼。合并单元格 python-docx 会把
    同一个单元格在跨列的位置上重复给出，重复的列名:值压掉一份，免得「阶段：beginner；
    阶段：beginner」这种噪声进 embedding。"""
    rows = [[c.text.strip() for c in r.cells] for r in tbl.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    if not body:
        return [" | ".join(h for h in header if h)]
    out: list[str] = []
    for row in body:
        parts: list[str] = []
        seen: set[tuple[str, str]] = set()
        for i, cell in enumerate(row):
            if not cell:
                continue
            key = header[i] if i < len(header) and header[i] else f"列{i + 1}"
            if (key, cell) in seen:
                continue
            seen.add((key, cell))
            parts.append(f"{key}：{cell}")
        if parts:
            out.append("；".join(parts))
    return out


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", text.strip()).strip("-")
    return s[:limit] or "untitled"


def slice_docx(name: str, raw: bytes, max_chars: int) -> list[tuple[list[str], list[str]]]:
    """一份 .docx → [(章节路径, 内容行), …]，顺序即文档顺序。"""
    doc = Document(io.BytesIO(raw))
    path: list[str] = []
    units: list[tuple[list[str], list[str]]] = []
    buf: list[str] = []
    n = 0

    def flush() -> None:
        nonlocal buf, n
        # 逐行只扔真正的垃圾（单字、页码）；表格摊出来的「列名：值」整行可以很短，
        # 不能按 MIN_CHARS 过滤，否则「级别：caution」这种会被连带丢掉。
        body = [x for x in buf if len(x) > 4]
        if sum(map(len, body)) >= MIN_CHARS:
            units.append((list(path) or [name], body))
            n += 1
        buf = []

    def emit(line: str) -> None:
        nonlocal buf
        # 单行自己就超长（大段正文），直接独立成片，不跟别人挤。
        if len(line) > max_chars:
            flush()
            units.append((list(path) or [name], [line]))
            n += 1
            return
        if buf and sum(map(len, buf)) + len(line) > max_chars:
            flush()
        buf.append(line)

    for block in _iter_blocks(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            level = _heading_level(block)
            if level:
                flush()
                path = path[: level - 1] + [text]
                emit(text)
            else:
                emit(text)
        elif isinstance(block, Table):
            for rec in _table_records(block):
                emit(rec)
    flush()
    return units


def write_doc(out_dir: Path, src: str, units: list[tuple[list[str], list[str]]]) -> str:
    """一个来源文件 → 一个 .docx；每行前面钉 `〔来源·章节〕` 前缀。"""
    stem = re.sub(r"\.docx$", "", src, flags=re.I)
    doc = Document()
    doc.add_heading(stem, level=1)
    doc.add_paragraph(
        f"来源：{src}｜{len(units)} 个片段｜由 containerd/tools/slice_kb_corpus.py 从项目方"
        "语料包按文档原顺序展开（表格逐行摊平成「列名：值」，每行钉章节前缀），原文没改字。"
    )
    for path, lines in units:
        tag = path[-1] if len(path) > 1 else ""
        prefix = f"〔{stem}·{tag}〕" if tag else f"〔{stem}〕"
        for line in lines:
            doc.add_paragraph(prefix + line)
    fname = f"{_slug(stem)}.docx"
    doc.save(str(out_dir / fname))
    return fname


def load_sources(spec: str) -> list[tuple[str, bytes]]:
    p = Path(spec)
    # 按内容而不是后缀判 zip：`./run.sh kbslice` 是把它 bind-mount 成 /src/in 的，
    # 容器里看不到 .zip 这个后缀。
    if p.is_file() and zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            return [
                (Path(n).name, z.read(n))
                for n in sorted(z.namelist())
                if n.lower().endswith(".docx") and not Path(n).name.startswith("~$")
            ]
    if not p.is_dir():
        raise SystemExit(f"既不是 .zip 也不是目录：{spec}")
    return [
        (f.name, f.read_bytes())
        for f in sorted(p.rglob("*.docx"))
        if not f.name.startswith("~$") and ".docx" not in f.parts
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="装 .docx 的目录，或语料包 .zip")
    ap.add_argument("out", nargs="?", default="seed/kb_corpus")
    ap.add_argument("--max-chars", type=int, default=900)
    ap.add_argument("--keep", action="store_true", help="不清空旧切片（默认清掉本工具上次写的）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prev = out_dir / MANIFEST

    sources = load_sources(args.src)
    if not sources:
        raise SystemExit(f"{args.src} 里没有 .docx")
    # 先把全部原文解析完再动旧文件：反过来的话，一次解析失败就把上一版切片清没了，
    # 而这份目录是进 git 的语料，不该出现「跑一半失败 → 目录空的」这种状态。
    sliced = [(name, raw, slice_docx(name, raw, args.max_chars)) for name, raw in sources]

    if not args.keep and prev.exists():
        old = json.loads(prev.read_text())
        for fname in old.get("files", []):
            (out_dir / fname).unlink(missing_ok=True)

    written: list[str] = []
    per_file: list[tuple[str, int, int]] = []
    total_units = 0
    for name, raw, units in sliced:
        chars = sum(sum(map(len, lines)) for _, lines in units)
        total_units += len(units)
        written.append(write_doc(out_dir, name, units))
        per_file.append((name, len(units), chars))
        print(f"{name:34s} → {len(units):4d} 片 · {chars:7d} 字")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "src": str(args.src),
        "max_chars": args.max_chars,
        "files": written,
        "sources": [
            {"name": n, "units": u, "chars": c,
             "sha256": hashlib.sha256(raw).hexdigest()[:16]}
            for (n, raw), (n2, u, c) in zip(sources, per_file)
        ],
    }
    prev.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    print(f"\n[切片] {len(sources)} 份原文 · {total_units} 个片段 → {len(written)} 个语料文件，清单 {prev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
