-- 同声传译的计费计数（与账户系统共用同一个 D1）
-- 用法：wrangler d1 execute aurora-account --remote --file=cf/live/schema.sql
CREATE TABLE IF NOT EXISTS live_meter (
  user_id    INTEGER PRIMARY KEY,
  seconds    REAL    NOT NULL DEFAULT 0,   -- 累计但还没满一分钟的语音秒数
  updated_at INTEGER NOT NULL
);
