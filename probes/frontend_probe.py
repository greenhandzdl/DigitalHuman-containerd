#!/usr/bin/env python3
"""真栈冒烟：从 CareEcho H5 的外壳打进去，看这一整串答不答得上来。

和 frontend-test 的分工：那组用假后端把外壳的每一条契约钉死（信封、cookie、会话复用、
故障语义），全绿也证明不了这套容器真的能答一句人话；这组只做一件事 —— 拿前端**实际会发**
的那个请求形状（`POST /api/chat/send`，只带 content 与 scene）穿过
外壳 → backend → adapter → Fay → 宿主 Ollama，看回不回得来。

只发一问。这一问在本机最坏是 172~301s（9b 权重驻留显存只剩 6% 时的实测），
后端侧的等待上限是 520s，默认 --timeout 600 就是照这条链给的余量。会话复用那条判据
在 frontend-test 里已经用假后端钉死了，这里不再花第二发问答去重复它。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request


def req(url: str, payload: dict | None = None, cookie: str | None = None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET",
                               headers={"Content-Type": "application/json",
                                        **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://frontend:8080")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--ask", default="我血压有点高，平时该注意什么？")
    args = ap.parse_args()

    ok = True
    # 1 静态产物真的在这个正在跑的实例里（不是只有镜像里有）
    st, body, _ = req(args.base + "/", timeout=30)
    good = st == 200 and "CareEcho" in body
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  1 外壳发出 H5 产物  —— HTTP {st}, {len(body)} 字节")

    # 2 健康口（compose 的 healthcheck 打的就是它）
    st, body, _ = req(args.base + "/api/health", timeout=30)
    good = st == 200 and json.loads(body).get("code") == 200
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  2 /api/health  —— HTTP {st} {body[:120]}")

    # 3 前端那一个真在用的口，整条链答上
    t0 = time.time()
    st, body, hdrs = req(args.base + "/api/chat/send",
                         {"content": args.ask, "scene": "consultation"}, timeout=args.timeout)
    dt = time.time() - t0
    try:
        got = json.loads(body)
    except json.JSONDecodeError:
        got = {}
    data = got.get("data") or {}
    reply = str(data.get("reply") or "")
    cookie = re.search(r"(carecho_device=[0-9a-f]{32})", hdrs.get("Set-Cookie") or "")
    good = (st == 200 and got.get("code") == 200 and len(reply.strip()) > 1
            and "连接失败" not in reply and bool(cookie))
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  3 POST /api/chat/send 走完整条链答上"
          f"  —— {dt:.1f}s，HTTP {st}，{len(reply)} 字，会话 {data.get('session_id')}，"
          f"风险分级 {data.get('risk_tier')}，cookie {'有' if cookie else '无'}")
    print(f"     问：{args.ask}\n     答：{reply[:220]}"
          + (f"\n     msg：{got.get('msg')}" if got.get("msg") else ""))

    print(f"\n[frontend-probe] {'三条全过' if ok else '有失败项'}（{dt:.1f}s 那一问是主要开销）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
