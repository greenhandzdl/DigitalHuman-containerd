-- 容器内 MySQL 初始化脚本（只在数据目录为空时执行一次）。
-- 库名与 service 的 MYSQL_DB 保持一致，字符集必须 utf8mb4，否则中文康养语料/表情会截断。
CREATE DATABASE IF NOT EXISTS `care_echo_rehab`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON `care_echo_rehab`.* TO 'root'@'%';
FLUSH PRIVILEGES;
