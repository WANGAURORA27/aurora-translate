-- 迁移 0001：users 增加「精修」能力位 can_refine
--
-- 为什么要单独走迁移而不是只改 schema.sql：schema.sql 全是 CREATE TABLE IF NOT EXISTS，
-- 对**已经上线**的库再跑一遍不会给旧表补列。线上库必须执行本文件。
--
-- 执行（在 cf/account 目录下，远程库）：
--   wrangler d1 execute aurora-account --remote --file=migrations/0001-add-can-refine.sql
-- 本地库：
--   wrangler d1 execute aurora-account --local --file=migrations/0001-add-can-refine.sql
--
-- 顺序很重要：**先跑迁移、再部署 Worker**。新 Worker 的会话查询会读 can_refine，
-- 列不存在会让两个站的登录都失败（旧 Worker 无视多出来的列，所以反过来不会出事）。
--
-- 注意：本文件不能重复执行（SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，
-- 重复跑会报 "duplicate column name: can_refine"）。重跑前先用
--   PRAGMA table_info(users); 确认列是否已存在。

ALTER TABLE users ADD COLUMN can_refine INTEGER NOT NULL DEFAULT 0;

-- 存量账号补齐：VIP 与管理员默认有这个能力。
-- 为什么不用触发器/默认值表达：角色的语义在应用层，放在这里只是一次性对齐数据，
-- 之后管理员在后台单独关掉某人的 can_refine 不会被这条语句再翻回来。
UPDATE users SET can_refine = 1 WHERE role IN ('vip', 'admin');
