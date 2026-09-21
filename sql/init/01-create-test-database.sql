-- 测试专用库：与业务库同实例、不同 schema，供 compose --profile test 的 backend-test 使用，
-- 这样 pytest 的建表/写数据动作不会落在业务库 care_echo_rehab 上。
-- 注意上游 service/tests/conftest.py 并不清表（81 行里一句 DELETE/TRUNCATE 都没有），
-- 而本栈的 mysql-data 卷跨轮存活，所以每轮跑 pytest 前由 containerd/tools/reset_test_db.py
-- 把这个库整个重建 —— 否则残留会一直攒（攒到第 14 轮时 test_human_queue_flow 就红了，
-- 详见 README「三种手段」的测试层）。这个文件只保证"库存在"，不承担清理职责。
CREATE DATABASE IF NOT EXISTS care_echo_rehab_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
