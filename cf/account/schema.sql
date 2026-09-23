-- aurora 账户系统 · 阶段 1 表结构（Cloudflare D1 / SQLite）
-- 应用方式：wrangler d1 execute aurora-account --file=cf/account/schema.sql --remote
-- 本文件可重复执行（都是 IF NOT EXISTS）。

-- ── 用户 ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT    NOT NULL UNIQUE,              -- 统一存小写
  pass_hash     TEXT    NOT NULL,                     -- pbkdf2$迭代$盐$哈希（绝不存明文）
  role          TEXT    NOT NULL DEFAULT 'user',      -- user / vip / admin
  status        TEXT    NOT NULL DEFAULT 'active',    -- active / banned
  quota_pages   INTEGER NOT NULL DEFAULT 200,         -- 每月可用页数（0 = 不限）
  used_pages    INTEGER NOT NULL DEFAULT 0,           -- 本月已用
  quota_reset_at INTEGER NOT NULL DEFAULT 0,          -- 上次重置用量的时间
  created_at    INTEGER NOT NULL,
  last_login_at INTEGER
);

-- ── 会话：只存 token 的 SHA-256，数据库泄露也不能直接拿来冒充 ──────────────
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT    PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  ua         TEXT,
  ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);

-- ── 邮箱验证码：同样只存哈希，10 分钟过期，用过即废 ────────────────────────
CREATE TABLE IF NOT EXISTS codes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT    NOT NULL,
  purpose    TEXT    NOT NULL,          -- register / reset
  code_hash  TEXT    NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at    INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codes_lookup ON codes(email, purpose, created_at);

-- ── 用量账本：阶段 2 接翻译站时，每翻一个文件写一条 ────────────────────────
CREATE TABLE IF NOT EXISTS usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  job_id     TEXT,
  kind       TEXT    NOT NULL DEFAULT 'translate',
  pages      INTEGER NOT NULL DEFAULT 0,
  tokens_in  INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  note       TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, created_at);

-- ── 审计日志：谁在什么时候做了什么（改权限、发额度、封号等）─────────────────
CREATE TABLE IF NOT EXISTS audit (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id   INTEGER,                   -- 操作者（系统操作为 NULL）
  target_id  INTEGER,
  action     TEXT    NOT NULL,
  detail     TEXT,
  ip         TEXT,
  created_at INTEGER NOT NULL
);

-- ── 邀请码（可选：注册时要求填邀请码，防止陌生人白用额度）──────────────────
CREATE TABLE IF NOT EXISTS invites (
  code       TEXT    PRIMARY KEY,
  created_by INTEGER,
  max_uses   INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER,
  created_at INTEGER NOT NULL
);
