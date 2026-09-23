/**
 * 账户系统与翻译站共用的会话/额度模块。
 *
 * 为什么要共用：doc.ourmetaverse.cn 和 account.ourmetaverse.cn 是两个独立 Worker，
 * 但它们**绑同一个 D1 数据库**、**共用同一个 Cookie**（Domain=.ourmetaverse.cn）。
 * 所以翻译站不需要回头去问账户站"这人是谁"，直接读会话表即可 —— 又快又没有单点依赖。
 *
 * 安全逻辑只有这一份，两边行为不会跑偏。
 */

export const SESSION_COOKIE = "aurora_session";

/** 读 Cookie */
export function parseCookies(request) {
  const out = {};
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k) out[k] = decodeURIComponent(rest.join("=") || "");
  }
  return out;
}

export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 定时安全比较 */
export function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const nowSec = () => Math.floor(Date.now() / 1000);

/** 跨月自动重置用量（惰性：每次读用户时顺手做一次，不需要定时任务） */
async function ensureMonthlyReset(env, user) {
  const now = nowSec();
  const last = Number(user.quota_reset_at) || 0;
  // 30 天为一个周期，够用且实现简单
  if (now - last < 30 * 24 * 3600) return user;
  await env.DB.prepare("UPDATE users SET used_pages = 0, quota_reset_at = ? WHERE id = ?")
    .bind(now, user.id).run();
  return { ...user, used_pages: 0, quota_reset_at: now };
}

/**
 * 取当前登录用户；没登录/会话过期/被停用都返回 null。
 * 需要 Worker 绑定 env.DB（同一个 D1）。
 */
export async function currentUser(env, request) {
  const token = parseCookies(request)[SESSION_COOKIE];
  if (!token) return null;
  const now = nowSec();
  const row = await env.DB.prepare(
    `SELECT s.token_hash AS token_hash, s.expires_at AS expires_at,
            u.id AS id, u.email AS email, u.role AS role, u.status AS status,
            u.quota_pages AS quota_pages, u.used_pages AS used_pages,
            u.quota_reset_at AS quota_reset_at, u.created_at AS created_at
       FROM sessions s JOIN users u ON u.id = s.user_id
      WHERE s.token_hash = ?`,
  ).bind(await sha256Hex(token)).first();

  if (!row || row.expires_at < now || row.status !== "active") return null;
  return ensureMonthlyReset(env, row);
}

/** 额度状态：不限 / 剩余页数 */
export function quotaState(user) {
  if (!user) return { unlimited: false, quota: 0, used: 0, remaining: 0 };
  if (Number(user.quota_pages) === 0) return { unlimited: true, quota: 0, used: Number(user.used_pages) || 0, remaining: Infinity };
  const quota = Number(user.quota_pages) || 0;
  const used = Number(user.used_pages) || 0;
  return { unlimited: false, quota, used, remaining: Math.max(0, quota - used) };
}

/** 记账：扣额度 + 写用量流水（翻译完成后调用） */
export async function addUsage(env, userId, { jobId, pages = 0, tokensIn = 0, tokensOut = 0, note = "", kind = "translate" }) {
  const pagesInt = Math.max(0, Math.round(Number(pages) || 0));
  const now = nowSec();
  await env.DB.prepare(
    `INSERT INTO usage (user_id, job_id, kind, pages, tokens_in, tokens_out, note, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(userId, jobId || null, kind, pagesInt, Math.round(tokensIn) || 0, Math.round(tokensOut) || 0,
         String(note || "").slice(0, 200), now).run();
  await env.DB.prepare("UPDATE users SET used_pages = used_pages + ? WHERE id = ?")
    .bind(pagesInt, userId).run();
  return pagesInt;
}

/** 设置会话 Cookie（Domain 让子域共享；清除时属性必须完全一致，否则删不掉） */
export function sessionCookie(token, maxAge, domain) {
  const d = domain ? `; Domain=${domain}` : "";
  return `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; Secure; SameSite=Lax${d}; Max-Age=${maxAge}`;
}
