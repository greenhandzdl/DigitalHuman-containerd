#!/usr/bin/env python3
"""`.env` → 容器的映射视图，由 `./run.sh env` 调用（也单独跑得动，便于复核）。

为什么要这么一条命令：容器读到的环境变量只有两处来源 —— compose 里 `environment:`
手写的键，和 `--env-file .env` 提供的那些 `${VAR}` 插值。**这两处对不上时不会报错**：
`.env` 里多出来的键只是没人读，改了等于没改。这台机器上真发生过 —— 切远端 26B 那次往
`.env` 写了 `FAY_GPT_MODEL_ENGINE=gemma-4-26b-a4b-nvfp4`，而 `docker exec dh-fay env`
里根本没有这个键（`x-llm-endpoint` 当时只透传了 base_url / api_key，漏了 patches/fay/0006
放开的模型名那两条），于是一段时间里"配好的模型名"其实一直是 system.conf 里那个 9b。
50 个 `${VAR}` 各自被谁读，靠人记住是做不到的，所以把对账写成代码。

三个口径分开报，因为它们要你做的动作完全不同：
  1. `.env` 每一行落到哪个服务（进容器的 / 只影响宿主端口与 hosts 的 / 没有任何服务读的）；
  2. compose 会读但 `.env` 没写的键 —— 这些走 compose 里 `:-` 后面那个默认值，想改就得
     **新增**这一行，而不是去改一个看着像、其实没接上的键；
  3. `.env.example` 里一个字没提的键 —— 模板不完整，新克隆的人根本不知道有这个 knob。

密钥类只报长度不报值（这份输出会留在终端里，也可能被截图拿去问人）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # containerd/

# 变量出现的段落 → 它到底进不进容器进程。environment/command 进的，ports 与
# extra_hosts 只决定宿主发布地址和容器内 hosts 里的名字。
IN_CONTAINER = ("environment", "command", "entrypoint")
HOST_SIDE_NOTE = {
    "ports": "只决定宿主端口绑在哪/发到哪个宿主口",
    "extra_hosts": "只写容器内的 hosts 名字",
}
# run.sh 自己读的键：不进容器，但确实有效 —— 不列出来就会被误报成"改了不生效"。
RUNSH_ONLY = {
    "DH_ENV": "run.sh 用它挑 compose 文件",
    "BIND_ADDR": "run.sh 与 compose 的 ports 用它，不进容器进程",
    "DH_LAN_IP": "run.sh 探测/覆盖；容器只在 FAY_URL 与 ue-host 里拿到它的值",
    "OLLAMA_PORT": "只有 run.sh smoke 拿它打印显存驻留比例",
}
SECRET_RE = re.compile(r"(PASSWORD|SECRET|API_KEY|TOKEN|_KEY$)")
SECTIONS = ("environment", "command", "entrypoint", "ports", "extra_hosts",
            "healthcheck", "depends_on", "build", "volumes", "image", "container_name")

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ANCHOR_RE = re.compile(r"&([a-z0-9-]+)")
MERGE_RE = re.compile(r"<<:\s*(\[[^\]]*\]|[a-z0-9-]+)")
# 整值就是一个插值（可以直接按名字传进容器）；否则是嵌在更大的字符串里。
WHOLE_RE = re.compile(r"^\s*\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[^}]*)?\}\s*$")


def scan(path: Path):
    """返回 (refs, svc_env, defaults)。

    refs[VAR]     -> 引用它的所有段落名
    svc_env[SVC]  -> {VAR: [(容器里的键名, 整值还是嵌入), ...]}
    defaults[VAR] -> `${VAR:-默认}` 里那个默认值
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    refs: dict[str, set[str]] = defaultdict(set)
    svc_env: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    defaults: dict[str, str] = {}

    # 第一遍只收顶层 `x-*: &anchor` 块里的键：第二遍展开 `<<: *anchor` 时要按名字取。
    anchors: dict[str, set[str]] = {}
    cur: str | None = None
    for raw in lines:
        line = raw.rstrip("\r")
        if line and not line.startswith((" ", "\t")):
            m = ANCHOR_RE.search(line)
            cur = m.group(1) if m else None
            continue
        if cur and "${" in line:
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*):", line)
            for var, dflt in VAR_RE.findall(line):
                anchors.setdefault(cur, set()).add((var, m.group(1) if m else var))
                if dflt:
                    defaults.setdefault(var, dflt)

    service: str | None = None
    section = ""
    in_services = False
    for raw in lines:
        line = raw.rstrip("\r")
        if line.startswith("services:"):
            in_services, service, section = True, None, ""
            continue
        if in_services and line and not line.startswith(" "):
            in_services = False      # 走到顶层 volumes: / networks: 了
        if not in_services:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 2 and ":" in stripped and not stripped.startswith("-"):
            service, section = stripped.split(":", 1)[0], ""
            if service == "profiles":
                service = None
            continue
        head = stripped.split(":", 1)[0] if ":" in stripped else ""
        if indent == 4 and head in SECTIONS:
            section = head
        where = section if section in ("environment", "command", "entrypoint",
                                       "ports", "extra_hosts") else (
            "其他" if indent >= 4 else "")
        for var, dflt in VAR_RE.findall(line):
            refs[var].add(where)
            if dflt:
                defaults[var] = dflt
            if service and where in IN_CONTAINER:
                key = head if where == "environment" else "(命令行)"
                rhs = stripped.split(":", 1)[1] if ":" in stripped else ""
                w = WHOLE_RE.match(re.sub(r"\s+#.*$", "", rhs))
                svc_env[service][var].append((key, "值" if (w and w.group(1) == var) else "嵌入"))
        m = MERGE_RE.fullmatch(stripped) if (service and where == "environment") else None
        if m:
            for name in re.findall(r"[a-z0-9-]+", m.group(1)):
                for var, key in anchors.get(name, set()):
                    svc_env[service][var].append((key, "值"))
                    refs[var].add("environment")
    return refs, svc_env, defaults


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def template_keys(path: Path) -> set[str]:
    """模板里提到过的键名，含 `# KEY=` 这种默认注释掉的 —— 注释掉也是一种记账。"""
    if not path.exists():
        return set()
    return set(re.findall(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=",
                          path.read_text(encoding="utf-8"), flags=re.MULTILINE))


def shown(key: str, value: str) -> str:
    if not value:
        return "（空 = 用默认）"
    if SECRET_RE.search(key):
        return f"<隐藏 {len(value)} 位>"
    return value if len(value) <= 44 else value[:41] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--compose", action="append", default=[], type=Path)
    ap.add_argument("--env", type=Path, default=HERE / ".env")
    ap.add_argument("--template", type=Path, default=HERE / ".env.example")
    ap.add_argument("--tier", default="", help="抬头显示用的档位名")
    args = ap.parse_args()

    refs: dict[str, set[str]] = defaultdict(set)
    svc_env: dict[str, dict[str, list]] = {}
    defaults: dict[str, str] = {}
    for cf in args.compose:
        r, s, d = scan(cf)
        for k, v in r.items():
            refs[k] |= v
        for svc, keys in s.items():
            for var, hits in keys.items():
                svc_env.setdefault(svc, {}).setdefault(var, []).extend(hits)
        for k, v in d.items():
            defaults.setdefault(k, v)

    env = parse_env_file(args.env)
    tpl = template_keys(args.template)
    consumers: dict[str, list[str]] = {}
    for svc, keys in svc_env.items():
        for var in keys:
            consumers.setdefault(var, []).append(svc)

    def where_text(var: str) -> str:
        """`→ 服务名(容器里叫 KEY / 嵌在 KEY 里)`，名字一致时不啰嗦。"""
        bits = []
        for svc in sorted(consumers.get(var, [])):
            aliases = []
            for key, kind in svc_env[svc][var]:
                if key == "(命令行)":
                    note = "作为启动命令的参数"
                elif key == var:
                    note = "" if kind == "值" else f"嵌在 {key}"
                else:
                    note = f"叫 {key}" if kind == "值" else f"嵌在 {key}"
                if note and note not in aliases:
                    aliases.append(note)
            bits.append(svc + (f"({', '.join(aliases)})" if aliases else ""))
        return "→ " + " ".join(bits)

    def host_text(var: str) -> str:
        side = "; ".join(sorted(HOST_SIDE_NOTE.get(s, s) for s in refs.get(var, ())))
        return f"· {side}（不进容器进程）" if side else "· 只在 run.sh 里用"

    width = max([len(k) for k in env] + [26])
    print(f"■ .env 每一行的去向  （.env 里写的档位 {args.tier or '?'} · 两份 compose 全算："
          f"变量表跨档位共用，档位只改端口发布 · 有插值变量的服务 {len(svc_env)} 个）")
    dead = []
    for key in sorted(env, key=lambda k: (k not in consumers, k.lower())):
        if key not in consumers:
            note = RUNSH_ONLY.get(key)
            if note:
                print(f"  {key:<{width}}  · {note}")
            elif refs.get(key):
                # compose 引用了它，但只用在 ports / extra_hosts 这类段落里：值确实生效，
                # 只是不变成容器里的环境变量 —— 这跟"没人读"是两回事，别混着报。
                side = "; ".join(sorted(HOST_SIDE_NOTE.get(s, s) for s in refs[key]))
                print(f"  {key:<{width}}  · {side}（不进容器进程）")
            else:
                print(f"  {key:<{width}}  ⚠ 没有任何服务读它 —— 改了不生效")
                dead.append(key)
            continue
        print(f"  {key:<{width}}  {where_text(key):<{width + 22}} {shown(key, env[key])}")
    if dead:
        print(f"    ↑ 这 {len(dead)} 个键（{', '.join(dead)}）compose 里没人引用："
              f"要么删掉，要么去 docker-compose.yml 把它接进对应服务的 environment:")
    else:
        print("    （.env 里每一行都有服务读，没有「改了不生效」的键）")

    unset = sorted(k for k in refs if k not in env)
    print(f"\n■ compose 会读、但 .env 没写的 {len(unset)} 个键"
          f"  （现在走下面那个 compose 默认值；要改就在 .env 里加这一行）")
    for key in unset:
        target = where_text(key) if consumers.get(key) else host_text(key)
        print(f"  {key:<{width}}  {target:<{width + 22}} 默认 {shown(key, defaults.get(key, ''))}")

    untpl = sorted(k for k in refs if k not in tpl)
    print(f"\n■ .env.example 里一个字没提的 {len(untpl)} 个键  （模板不完整 = 新克隆的人看不见）")
    for key in untpl:
        print(f"  {key:<{width}}  {where_text(key) if consumers.get(key) else '· 只影响宿主端口/hosts'}")
    if not untpl:
        print("  （没有：compose 引用的每个键，模板里都有一行）")
    return 0


if __name__ == "__main__":
    # 这份输出就是给人 grep / head 的，被截断不该以一段 traceback 收尾。
    try:
        sys.exit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
