#!/usr/bin/env python3
"""把 pytest 专用的测试库整个删掉重建 —— 让每一轮 backend-test 都从零开始。

为什么要这么干：上游 `service/tests/conftest.py`（全文 81 行）**一处清表都没有**，
每个 fixture 都用随机 openid 走 dev-login 新建用户（`openid = f"pytest_{uuid4}"`），
留下的数据全落在 `care_echo_rehab_test` 里。本栈的 MySQL 数据在卷上，测试库跨轮存活，
于是这些残留会一直累积 —— 实测到第 14 轮攒下 631 个用户，`test_human_queue_flow`
就红了：

    join 排队（risk_tier=medium，priority 低）
    → GET /volunteer/human-queue/pending
    → 应用层是 list_pending_queues(... limit=50)，ORDER BY priority DESC, enqueued_at ASC
    → 前 50 名被历轮留下的 pending 行占满，本轮这条排在 50 名之外，断言
      `any(item["id"] == queue_id ...)` 自然 False

这不是容器集成坏了，是「测试库必须是空的」这条上游隐含前提被我们的持久卷打破了。
上游 CI 每次都是新建库，所以它那边不会红。修法只能在我们这侧：起测试之前先把库重建，
不碰上游一行测试代码。

安全：DROP DATABASE 的库名不能走参数占位符（SQL 里标识符不能用 %s），所以这里对库名
做了严格白名单校验 —— 只认「纯 [a-z0-9_] 且以 _test 结尾」，其它一律拒绝并退出 2。
业务库 `care_echo_rehab` 不满足这个形状，永远不可能被这个脚本删掉。

用法：python reset_test_db.py（读 MYSQL_HOST/PORT/USER/PASSWORD/MYSQL_DB）
退出码：0 重建完成（含"本来就是空的"），2 拒绝执行（库名形状不对/连不上）。
"""
from __future__ import annotations

import os
import re
import sys

import pymysql

SAFE_NAME = re.compile(r"^[a-z0-9_]{1,32}_test$")
# 与 containerd/sql/init/01-create-test-database.sql 保持一致：utf8mb4 才装得下中文康养语料。
CHARSET = "utf8mb4"
COLLATION = "utf8mb4_unicode_ci"


def main() -> int:
    db = os.environ.get("MYSQL_DB", "")
    if not SAFE_NAME.match(db):
        print(
            f"[reset] 拒绝执行：MYSQL_DB={db!r} 不像测试库名"
            f"（要求 ^[a-z0-9_]{{1,32}}_test$）。业务库必须走别的流程",
            file=sys.stderr,
        )
        return 2
    host = os.environ.get("MYSQL_HOST", "mysql")
    port = int(os.environ.get("MYSQL_PORT", "3306"))
    conn = pymysql.connect(
        host=host,
        port=port,
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
    )
    with conn.cursor() as cur:
        # 先量一下丢的是什么，好让日志能回答「这轮为什么和上轮不一样」。
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s", (db,)
        )
        tables = int(cur.fetchone()[0] or 0)
        rows = 0
        if tables:
            cur.execute(
                "SELECT COALESCE(SUM(table_rows), 0) FROM information_schema.tables"
                " WHERE table_schema = %s",
                (db,),
            )
            # information_schema.table_rows 对 InnoDB 是估算值，这里只当量级看。
            rows = int(cur.fetchone()[0] or 0)
        cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        cur.execute(f"CREATE DATABASE `{db}` CHARACTER SET {CHARSET} COLLATE {COLLATION}")
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s", (db,)
        )
        left = int(cur.fetchone()[0] or 0)
    conn.close()
    if left:
        print(f"[reset] 建完还有 {left} 张表，不像空库 —— 后面别跑了", file=sys.stderr)
        return 2
    print(
        f"[reset] 测试库 {db} 已重建（丢掉 {tables} 张表 / 约 {rows} 行残留，估算值）"
        f"，字符集 {CHARSET}/{COLLATION} 与 sql/init 一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
