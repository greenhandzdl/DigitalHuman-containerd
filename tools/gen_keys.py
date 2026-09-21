#!/usr/bin/env python3
"""生成 containerd/.env：从 .env.example 复制并填入随机密钥，其余配置原样保留。

只写本地 .env（已被 containerd/.gitignore 排除），不改动模板、不落任何实值到 stdout。
密钥长度与 run.sh 原先内联的 openssl rand -hex 保持一致：
  MYSQL_ROOT_PASSWORD / REDIS_PASSWORD = 16 字节 → 32 位十六进制
  JWT_SECRET_KEY                        = 32 字节 → 64 位十六进制
"""
import argparse
import re
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # containerd/
TEMPLATE = HERE / ".env.example"
TARGET = HERE / ".env"

# key -> 随机字节数
KEYS = {
    "MYSQL_ROOT_PASSWORD": 16,
    "REDIS_PASSWORD": 16,
    "JWT_SECRET_KEY": 32,
}


def render(text: str) -> str:
    """把模板里每个 KEY=... 行的值替换成新的随机十六进制串。"""
    for key, nbytes in KEYS.items():
        value = secrets.token_hex(nbytes)
        text, n = re.subn(
            rf"^{re.escape(key)}=.*$", f"{key}={value}", text, count=1, flags=re.MULTILINE
        )
        if n == 0:
            print(f"[gen_keys] 警告：模板缺少 {key} 行，未填充", file=sys.stderr)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="生成填入随机密钥的 containerd/.env")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 .env")
    ap.add_argument(
        "--stdout",
        action="store_true",
        help="只把结果打到标准输出（含实值密钥），不落盘——仅供人工核对",
    )
    args = ap.parse_args()

    if not TEMPLATE.exists():
        print(f"[gen_keys] 找不到模板 {TEMPLATE}", file=sys.stderr)
        return 1

    rendered = render(TEMPLATE.read_text(encoding="utf-8"))

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if TARGET.exists() and not args.force:
        print(f"[gen_keys] 已存在 {TARGET}，保持不变（需要重生成加 --force）")
        return 0

    TARGET.write_text(rendered, encoding="utf-8")
    TARGET.chmod(0o600)  # 收紧到仅属主可读写
    print(f"[gen_keys] 已生成 {TARGET}（随机 DB 密码与 JWT 密钥，权限 600）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
