#!/usr/bin/env python3
"""从 overlay/*/*.json.example 生成本地那份 —— 与 tools/gen_keys.py 同一条规矩。

为什么这三份要模板化：它们是**可写** bind-mount 的源文件，Fay 运行期会整份重写回来。
实测三种噪声，每一种都不是人改的，但都会把 `git status` 弄脏、并且有机会被顺手提交：
  * mcp_servers.json —— faymcp/mcp_service.py:132 每次连接/断开都刷新 connection_time。
  * mcp_prestart_tools.json —— prestart_registry.py 的 json.dump 不补末尾换行。
  * config.json —— 控制台一保存，中文全被 json.dump 默认 ensure_ascii=True 写成 \\uXXXX。
仓库里只留可读的模板（UTF-8 原字、带人写的换行），实值留在本地：与「本地配置文件只给
示例，其他都写 .env」是同一条口径的两次应用。

本脚本由 run.sh 的 ensure_env 调用，所以覆盖到每一条会起容器的命令。它**只在目标缺失时
创建**，存在就一个字都不动 —— 已经调好的设定不会因为重跑一次 up 被抹掉。反过来说，改了
模板必须 --force 才落到本地那份。
"""
import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # containerd/


def pairs():
    for tpl in sorted(HERE.glob("overlay/*/*.json.example")):
        yield tpl, tpl.with_name(tpl.name[: -len(".example")])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="用模板覆盖本地那份（会丢掉 Fay 回写进去的运行期改动）")
    args = ap.parse_args()

    made = 0
    for tpl, target in pairs():
        if target.exists() and not args.force:
            continue
        target.write_bytes(tpl.read_bytes())
        made += 1
        verb = "已按模板重建" if args.force else "已生成"
        print(f"[gen_overlay] {verb} {target.relative_to(HERE)}（本地文件，不入库）")
    if args.force and made:
        print("[gen_overlay] 这些文件是 Fay 运行期会回写的那几份，覆盖等于把控制台里改的"
              "设定一起退回模板值", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
