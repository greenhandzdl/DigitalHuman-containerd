#!/usr/bin/env python3
"""ue/ 仓库的容器内体检（只读，不改上游工程）。

结论要回答一个具体问题：**这个仓库能不能像 fay/service 一样在容器里跑起来？**
判据不靠印象，靠扫出来的事实：

  1. 有没有可执行入口（.uproject 之外是否有 Source/ C++ 或 Blueprint 以外的代码）
  2. 依赖的插件里有多少是 UE 引擎自带之外的 Marketplace 授权插件
  3. 有多少插件只带 Windows 二进制（第三方 SDK 的 .lib/.dll 平台分布）
  4. 全树是否引用 Fay 的数字人协议（端口 10002 / Topic:"human" / Data.Key 等）

这四条只是事实陈列，判不了红 —— 旧版因此退出码恒为 0，"ue-audit 过了"等于没内容。
下面两条是**不装引擎也能判对错**的构建完整性：

  5. 工程描述符可解析   每个 `.uproject`/`.uplugin` 逐个 JSON 解析。这里有个坑：
                       XunFei / XunFeiTTS 的 .uplugin 是 **UTF-16 LE 带 BOM**（首字节
                       0xFF 0xFE），`read_text()` 直接抛 UnicodeDecodeError。UE 自己读得
                       懂（FFileHelper::LoadFileToString 按 BOM 定字符集），所以那不是坏
                       文件；判据必须先按 BOM 选编码，否则会把"我不会读"报成"文件坏"。
  6. 插件模块的 Source 都在  `plugins/**` 下每个自带 .uplugin 声明的 `Modules[].Name`，
                       都必须有同级的 `Source/<Name>/` 目录 —— 缺一个这个模块就编不出来。
                       只判 plugins/ 下的：`Config/Marketplace/` 里那几份是领授权用的副本，
                       不参与构建、旁边本来也没有 Source/，拿去判就是假红。

用法：python ue_audit.py /ue [--json out.json]
退出码：第 5/6 条任一 FAIL = 1，根目录不存在 = 2，其余 0。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Fay 侧数字人协议的关键字（core/wsa_server.py 的帧格式与端口）。
# 只收"协议级"标记：端口号、ws scheme、Topic:human 帧、UE 侧接收字段。
# 像 wave / "Key" 这种泛用词不能进来 —— .uasset 是二进制，用 errors="ignore"
# 硬啃出来的命中毫无意义（第一版就这么把"零引用"错判成了"38 处引用"）。
#
# 即便只看协议级标记，"10002" 仍然会被第三方头文件里的枚举值、MetaHuman 模板里
# 的 mesh 顶点索引撞上。所以每个标记改成正则 + 抓出命中行本身，让结论可被逐条否证。
PROTOCOL_PATTERNS = {
    "10002": r"\b10002\b",
    "ws-endpoint": r"(?:wss?|tcp|udp|http[s]?)://[^\s\"'<>]*\b10002\b",
    "port-kv": r"(?i)\b(port|端口)\b[\"']?\s*[:=]\s*[\"']?10002\b",
    "Topic:human": r"""(?i)['"]topic['"]\s*[:=]\s*['"]human['"]""",
    "MoveParameter": r"\bMoveParameter\b",
    "WebSocket": r"\b[Ww]eb[Ss]ocket\b",
}
# 真正的文本文件；UE 资产（.uasset/.umap/.upsmap）是二进制，一律不读内容
TEXT_SUFFIXES = {".py", ".js", ".ts", ".json", ".ini", ".cs", ".h", ".cpp", ".uproject",
                 ".uplugin", ".txt", ".md", ".xml", ".yml", ".yaml", ".bat", ".sh", ".uprojectfilters"}
MAX_SCAN_BYTES = 4 * 1024 * 1024  # 单文件超过 4MB 不读（UE 资产是二进制）
DESCRIPTOR_SUFFIXES = {".uproject", ".uplugin"}


def load_descriptor(path: Path) -> tuple[dict, bool]:
    """按 BOM 选编码解析 UE 的工程/插件描述符；第二个返回值是"它是 UTF-16"。

    UE 这两种文件都不保证是 UTF-8：XunFei 那两个是 Visual Studio 存的 UTF-16 LE。
    """
    raw = path.read_bytes()
    utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    body = json.loads(raw.decode("utf-16")) if utf16 else json.loads(raw.decode("utf-8-sig"))
    return body, utf16


def check_descriptors(root: Path, descriptors: list[Path]) -> tuple[list[tuple[str, str, str]], dict]:
    """第 5/6 条判据。解析结果顺带交回调用方，免得为同样的文件再走一遍树。"""
    out: list[tuple[str, str, str]] = []
    parsed: dict[Path, dict] = {}
    broken: list[str] = []
    utf16: list[str] = []
    for d in sorted(descriptors):
        try:
            body, is_utf16 = load_descriptor(d)
        except Exception as e:  # noqa: BLE001 - 读不出内容就是判据，不是 traceback
            broken.append(f"{d.relative_to(root)}: {type(e).__name__}: {str(e)[:60]}")
            continue
        parsed[d] = body
        if is_utf16:
            utf16.append(str(d.relative_to(root)))

    name = "工程描述符可解析"
    if not descriptors:
        out.append(("SKIP", name, "没找到任何 .uproject/.uplugin，没有可判的东西"))
    elif broken:
        out.append(("FAIL", name,
                    f"{len(descriptors) - len(broken)}/{len(descriptors)} 可解析，坏在 {broken[:4]}"))
    else:
        out.append(("PASS", name,
                    f"{len(descriptors)} 个全可解析（其中 {len(utf16)} 个是 UTF-16 带 BOM，"
                    f"必须按 BOM 才读得出来：{utf16}）"))

    name = "插件模块的 Source 目录都在"
    buildable = {d: b for d, b in parsed.items()
                 if d.suffix == ".uplugin" and d.relative_to(root).parts[:1] == ("plugins",)}
    missing: list[str] = []
    total = 0
    for d, body in sorted(buildable.items()):
        for mod in body.get("Modules", []) or []:
            mod_name = mod.get("Name")
            if not mod_name:
                continue
            total += 1
            if not (d.parent / "Source" / mod_name).is_dir():
                missing.append(f"{d.parent.relative_to(root)}→{mod_name}")
    if not total:
        out.append(("SKIP", name,
                    f"没有可判的模块：plugins/ 下自带 .uplugin {len(buildable)} 个，"
                    f"它们声明的 Modules 共 0 个"))
    elif missing:
        out.append(("FAIL", name, f"{total - len(missing)}/{total} 命中，缺 {missing[:5]}"))
    else:
        out.append(("PASS", name,
                    f"{len(buildable)} 个自带插件、{total} 个模块全部有对应的 Source/ 目录"
                    f"（Config/Marketplace 下的授权副本不参与构建，不判）"))
    return out, parsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="/ue")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"[ue-audit] 目录不存在: {root}", file=sys.stderr)
        return 2

    ext_count: Counter[str] = Counter()
    total_bytes = 0
    total_files = 0
    source_dirs: list[str] = []
    plugin_names: list[str] = []
    descriptors: list[Path] = []
    win_only_libs = 0
    other_libs = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        total_files += 1
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total_bytes += size
        ext_count[path.suffix.lower()] += 1
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() in DESCRIPTOR_SUFFIXES:
            descriptors.append(path)
        # UE 的 C++ 模块：任何 <Module>/Source/ 目录。取 Source 前那一段作为模块名，
        # 顶层记成 "<plugins>/X/Y"，其余记相对首段。
        if "/Source/" in f"/{rel}" and path.suffix.lower() in {".h", ".cpp", ".cs"}:
            module = rel.split("/Source/")[0].rsplit("/", 1)[-1]
            source_dirs.append(f"{rel.split('/')[0]}/{module}" if rel.startswith("plugins") else module)
        if path.suffix.lower() in {".lib", ".dll"}:
            win_only_libs += 1
        elif path.suffix.lower() in {".so", ".a"}:
            other_libs += 1

    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        for entry in sorted(plugins_dir.iterdir()):
            if entry.is_dir():
                for sub in sorted(entry.iterdir()):
                    if sub.is_dir():
                        plugin_names.append(f"{entry.name}/{sub.name}")

    # 协议引用扫描：只读真正的文本文件，命中的那一行原样留证
    hits: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {}
    scanned = 0
    compiled = {name: re.compile(pat) for name, pat in PROTOCOL_PATTERNS.items()}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        rel = path.relative_to(root).as_posix()
        for name, pattern in compiled.items():
            for match in pattern.finditer(text):
                hits[name] += 1
                lines = evidence.setdefault(name, [])
                if len(lines) < 6:
                    start = text.rfind("\n", 0, match.start()) + 1
                    end = text.find("\n", match.end())
                    snippet = text[start:end if end > start else len(text)].strip()
                    lines.append(f"{rel}: {snippet[:160]}")

    verdicts, parsed = check_descriptors(root, descriptors)

    report = {
        "root": str(root),
        "total_files": total_files,
        "total_gib": round(total_bytes / 1024 ** 3, 2),
        "top_extensions": ext_count.most_common(12),
        "modules_with_source": sorted(set(source_dirs)),
        "plugins": plugin_names,
        "windows_binaries": win_only_libs,
        "unix_binaries": other_libs,
        "text_files_scanned": scanned,
        "protocol_pattern_hits": dict(hits),
        "protocol_pattern_evidence": evidence,
        "verdicts": [{"kind": k, "name": n, "detail": t} for k, n, t in verdicts],
    }
    report["engine_present"] = any((root / p).exists() for p in ("Engine", "../UE_5.0"))
    report["has_buildable_source"] = bool(source_dirs)
    # 只有"像端点"的命中才算引用了 Fay 的数字人协议：ws/http 端点、port=10002、
    # Topic:'human' 帧、UE 侧的 MoveParameter 接收字段。裸 10002 / WebSocket 是噪声。
    endpoint_keys = ("ws-endpoint", "port-kv", "Topic:human", "MoveParameter")
    report["fay_protocol_referenced"] = {k: hits[k] for k in endpoint_keys if hits.get(k)}

    uprojects = {d: b for d, b in parsed.items() if d.suffix == ".uproject"}
    report["engine_association"] = next(
        (b.get("EngineAssociation") for b in uprojects.values() if b.get("EngineAssociation")), None)
    # .uproject 声明的每个插件：能在仓库里找到同名 .uplugin 的叫"随仓库带上了"；找不到的
    # 不是错 —— 那是引擎自带插件，或要在 Marketplace 领授权的东西，容器里没有引擎，
    # 判不了它对不对，所以只列出来，不进判据。
    declared = [p.get("Name") for b in uprojects.values() for p in b.get("Plugins", []) or []
                if p.get("Name") and p.get("Enabled", True)]
    local = {d.stem: d.relative_to(root).as_posix() for d in sorted(parsed) if d.suffix == ".uplugin"}
    report["declared_plugins"] = declared
    report["plugins_resolved_in_repo"] = {n: local[n] for n in declared if n in local}
    report["plugins_not_resolved_in_repo"] = [n for n in declared if n not in local]
    # 盘上有、工程没声明：不判红（作者可能就是先把它摘下来了），但要让这件事可见。
    report["plugins_on_disk_not_declared"] = sorted(
        n for n in local if n not in declared and local[n].startswith("plugins/"))

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.json:
        Path(args.json).write_text(payload, encoding="utf-8")

    n = Counter(k for k, _, _ in verdicts)
    print("\n[ue-audit] 判读：", file=sys.stderr)
    for kind, name, detail in verdicts:
        print(f"  {kind}  {name}" + (f"  —— {detail}" if detail else ""), file=sys.stderr)
    print(f"  [ue-audit] {n['PASS']} PASS / {n['SKIP']} SKIP / {n['FAIL']} FAIL", file=sys.stderr)
    print(f"  要 UE {report['engine_association']}；声明 {len(declared)} 个插件，"
          f"仓库自带 {len(report['plugins_resolved_in_repo'])} 个，"
          f"得在引擎/Marketplace 那边的 {report['plugins_not_resolved_in_repo']}", file=sys.stderr)
    print(f"  盘上有但工程没声明：{report['plugins_on_disk_not_declared'] or '无'}", file=sys.stderr)
    print(f"  仓库 {report['total_gib']} GiB / {total_files} 文件；"
          f"自带引擎={report['engine_present']}；带 Source 的模块={len(set(source_dirs))}", file=sys.stderr)
    print(f"  插件目录 {len(plugin_names)} 个；Windows 二进制(.lib/.dll) {win_only_libs} 个，"
          f"Unix 二进制(.so/.a) {other_libs} 个", file=sys.stderr)
    print(f"  裸标记：10002={hits.get('10002', 0)} 次、WebSocket={hits.get('WebSocket', 0)} 次"
          f"（扫描 {scanned} 个文本文件）", file=sys.stderr)
    print(f"  端点级引用：{report['fay_protocol_referenced'] or '无'}", file=sys.stderr)
    for key in endpoint_keys:
        for line in evidence.get(key, []):
            print(f"    {line}", file=sys.stderr)
    print("  裸 10002 的命中行长这样（用来判断是不是枚举值/顶点索引）：", file=sys.stderr)
    for line in evidence.get("10002", []):
        print(f"    {line}", file=sys.stderr)
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
