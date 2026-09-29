/**
 * aurora 账户系统 · 阶段 1（Cloudflare Worker + D1）
 *
 * 部署在 account.ourmetaverse.cn。负责：
 *   注册（邮箱 + 验证码）· 登录 · 退出 · 忘记密码 · 个人中心 · 权限 · 用量额度
 *
 * ── 安全设计（这些不是可选项）───────────────────────────────────────────
 * 1. 密码：PBKDF2-SHA256，随机 16 字节盐，10 万次迭代（WebCrypto 原生，无第三方库）
 * 2. 会话：Cookie 里放 32 字节随机 token，**数据库里只存它的 SHA-256**
 *           —— 数据库万一泄露，别人也没法直接拿来冒充
 * 3. 验证码：只存 HMAC 哈希，10 分钟过期，用过即废，同邮箱/同 IP 都限流
 * 4. Cookie：HttpOnly + Secure + SameSite=Lax（JS 读不到，跨站带不出去）
 * 5. 所有写操作要求同源（校验 Origin）+ POST + JSON
 * 6. 登录失败不区分"邮箱不存在"和"密码错"，避免被用来枚举用户
 * 7. 敏感操作写审计日志
 */

import { PAGE } from "./page.js";
// 会话/额度逻辑与翻译站共用同一份（见 cf/shared/session.js），避免安全代码抄两遍
import { SESSION_COOKIE, parseCookies, sha256Hex, safeEqual, currentUser, sessionCookie }
  from "../shared/session.js";

const SESSION_TTL = 30 * 24 * 3600; // 30 天
const CODE_TTL = 10 * 60; // 验证码 10 分钟
const PBKDF2_ITER = 100000;
const MIN_PASSWORD = 8;
// 各角色的每月默认页数（管理员可在后台单独调整某个用户）
const ROLE_QUOTA = { admin: 200, vip: 50, user: 30 };
const DEFAULT_QUOTA = ROLE_QUOTA.user;

// ── 小工具 ────────────────────────────────────────────────────────────

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}
const ok = (data = {}) => json({ ok: true, ...data });
const fail = (message, status = 400) => json({ ok: false, error: message }, status);

function b64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function randomToken(bytes = 32) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return b64(buf).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const normEmail = (v) => String(v || "").trim().toLowerCase();
const isEmail = (v) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) && v.length <= 254;

// ── 密码 ──────────────────────────────────────────────────────────────

async function hashPassword(password) {
  const salt = new Uint8Array(16);
  crypto.getRandomValues(salt);
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITER, hash: "SHA-256" },
    key, 256,
  );
  return `pbkdf2$${PBKDF2_ITER}$${b64(salt)}$${b64(bits)}`;
}

async function verifyPassword(password, stored) {
  const parts = String(stored || "").split("$");
  if (parts.length !== 4 || parts[0] !== "pbkdf2") return false;
  const iter = Number(parts[1]) || PBKDF2_ITER;
  let salt;
  try {
    salt = Uint8Array.from(atob(parts[2]), (c) => c.charCodeAt(0));
  } catch (err) {
    return false;
  }
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: iter, hash: "SHA-256" },
    key, 256,
  );
  return safeEqual(b64(bits), parts[3]);
}

// ── 验证码（只存哈希）──────────────────────────────────────────────────

async function codeHash(env, email, purpose, code) {
  // 掺进服务端密钥：即使数据库泄露，也没法离线暴力破解 6 位数字
  return sha256Hex(`${email}|${purpose}|${code}|${env.SESSION_SECRET || "no-secret"}`);
}

async function issueCode(env, email, purpose) {
  const code = String(Math.floor(100000 + Math.random() * 900000));
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare(
    "INSERT INTO codes (email, purpose, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
  ).bind(email, purpose, await codeHash(env, email, purpose, code), now + CODE_TTL, now).run();
  return code;
}

async function consumeCode(env, email, purpose, code) {
  const now = Math.floor(Date.now() / 1000);
  const row = await env.DB.prepare(
    `SELECT id, code_hash, expires_at FROM codes
      WHERE email = ? AND purpose = ? AND used_at IS NULL
      ORDER BY id DESC LIMIT 1`,
  ).bind(email, purpose).first();
  if (!row) return "验证码不存在或已使用，请重新获取";
  if (row.expires_at < now) return "验证码已过期，请重新获取";
  if (!safeEqual(row.code_hash, await codeHash(env, email, purpose, code))) return "验证码不对";
  await env.DB.prepare("UPDATE codes SET used_at = ? WHERE id = ?").bind(now, row.id).run();
  return null;
}

// ── 邀请码 ────────────────────────────────────────────────────────────

/** 生成一个不容易看错的邀请码：去掉 I O 0 1 */
function newInviteCode() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const buf = new Uint8Array(8);
  crypto.getRandomValues(buf);
  let body = "";
  for (const b of buf) body += chars[b % chars.length];
  return "AURORA-" + body.slice(0, 4) + "-" + body.slice(4, 8);
}

/** 只检查不消费（先校验、再消费，避免把验证码白白用掉） */
async function checkInvite(env, code) {
  const now = Math.floor(Date.now() / 1000);
  const row = await env.DB.prepare(
    "SELECT code, max_uses, used_count, expires_at FROM invites WHERE code = ?",
  ).bind(code).first();
  if (!row) return "邀请码不存在";
  if (row.expires_at && row.expires_at < now) return "邀请码已过期";
  if (row.used_count >= row.max_uses) return "邀请码已被用完";
  return null;
}

async function consumeInvite(env, code) {
  await env.DB.prepare("UPDATE invites SET used_count = used_count + 1 WHERE code = ?").bind(code).run();
}

// ── 限流（KV 计数器，按邮箱与 IP 双维度）────────────────────────────────

async function bumpLimit(env, key, ttl) {
  const now = Math.floor(Date.now() / 1000);
  const rec = (await env.LIMITS.get(key, "json")) || { n: 0, t: now };
  if (now - rec.t > ttl) {
    rec.n = 0;
    rec.t = now;
  }
  rec.n += 1;
  await env.LIMITS.put(key, JSON.stringify(rec), { expirationTtl: Math.max(ttl, 60) });
  return rec.n;
}

async function tooMany(env, keys) {
  // keys: [[key, 上限, 窗口秒], ...]
  for (const [key, max, ttl] of keys) {
    const n = await bumpLimit(env, key, ttl);
    if (n > max) return true;
  }
  return false;
}

// ── 会话 ──────────────────────────────────────────────────────────────

async function createSession(env, userId, request) {
  const token = randomToken(32);
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare(
    "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, ua, ip) VALUES (?, ?, ?, ?, ?, ?)",
  ).bind(
    await sha256Hex(token), userId, now, now + SESSION_TTL,
    (request.headers.get("user-agent") || "").slice(0, 200),
    request.headers.get("cf-connecting-ip") || "",
  ).run();
  return token;
}

async function audit(env, actorId, targetId, action, detail, request) {
  await env.DB.prepare(
    "INSERT INTO audit (actor_id, target_id, action, detail, ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
  ).bind(
    actorId || null, targetId || null, action, String(detail || "").slice(0, 300),
    (request && request.headers.get("cf-connecting-ip")) || "", Math.floor(Date.now() / 1000),
  ).run().catch(() => {});
}

// ── 邮件 ──────────────────────────────────────────────────────────────

async function sendMail(env, to, subject, text) {
  if (env.RESEND_KEY) {
    const resp = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { authorization: "Bearer " + env.RESEND_KEY, "content-type": "application/json" },
      body: JSON.stringify({
        from: env.MAIL_FROM || "Aurora <no-reply@ourmetaverse.cn>",
        to: [to], subject, text,
      }),
    });
    if (!resp.ok) {
      const t = await resp.text();
      return "发信失败（" + resp.status + "）：" + t.slice(0, 200);
    }
    return null;
  }
  if (env.DEV_SHOW_CODE === "1") return null; // 本地开发：验证码直接回给调用方
  return "服务端还没配置发信（缺少 RESEND_KEY）";
}

// ── 请求辅助 ──────────────────────────────────────────────────────────

/** 写操作必须同源：防 CSRF（配合 SameSite=Lax 双保险） */
function sameOrigin(request, url) {
  const origin = request.headers.get("origin");
  if (!origin) return true; // 命令行/服务端调用没有 Origin，靠 Cookie 本身约束
  try {
    const o = new URL(origin);
    return o.host === url.host;
  } catch (err) {
    return false;
  }
}

async function readJson(request) {
  const len = Number(request.headers.get("content-length") || 0);
  if (len > 4096) return null;
  try {
    return await request.json();
  } catch (err) {
    return null;
  }
}

// ── 接口 ──────────────────────────────────────────────────────────────

async function apiSendCode(request, env, url) {
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const email = normEmail(body.email);
  const purpose = body.purpose === "reset" ? "reset" : "register";
  if (!isEmail(email)) return fail("邮箱格式不对");

  const ip = request.headers.get("cf-connecting-ip") || "0";
  if (await tooMany(env, [
    ["rl:code:e:" + email, 1, 60],          // 同邮箱 1 分钟 1 条
    ["rl:code:e:d:" + email, 10, 86400],    // 同邮箱 1 天 10 条
    ["rl:code:ip:" + ip, 20, 86400],        // 同 IP 1 天 20 条
  ])) return fail("发送太频繁了，请等一分钟再试", 429);

  const exists = await env.DB.prepare("SELECT id FROM users WHERE email = ?").bind(email).first();
  if (purpose === "register" && exists) return fail("这个邮箱已经注册过了，直接登录吧");
  if (purpose === "reset" && !exists) return ok({ sent: true }); // 不暴露邮箱是否存在

  const code = await issueCode(env, email, purpose);
  const subject = purpose === "register" ? "Aurora 注册验证码" : "Aurora 重置密码验证码";
  const text = `你的验证码是 ${code}，10 分钟内有效。\n如果不是你本人操作，忽略这封邮件即可。`;
  const err = await sendMail(env, email, subject, text);
  if (err) return fail(err, 502);
  return ok({ sent: true, ...(env.DEV_SHOW_CODE === "1" ? { dev_code: code } : {}) });
}

async function apiRegister(request, env, url) {
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const email = normEmail(body.email);
  const code = String(body.code || "").trim();
  const password = String(body.password || "");
  if (!isEmail(email)) return fail("邮箱格式不对");
  if (password.length < MIN_PASSWORD) return fail(`密码至少 ${MIN_PASSWORD} 位`);
  if (!/^\d{6}$/.test(code)) return fail("验证码是 6 位数字");

  const ip = request.headers.get("cf-connecting-ip") || "0";
  if (await tooMany(env, [["rl:reg:ip:" + ip, 5, 86400]])) return fail("今天注册太多次了", 429);

  const exists = await env.DB.prepare("SELECT id FROM users WHERE email = ?").bind(email).first();
  if (exists) return fail("这个邮箱已经注册过了");

  const now = Math.floor(Date.now() / 1000);
  const isFirst = !(await env.DB.prepare("SELECT id FROM users LIMIT 1").first());

  // 邀请码：填了就是 VIP；站点主人也可以用 REQUIRE_INVITE=1 把它变成"口令"（必填才能注册）
  const invite = String(body.invite || "").trim().toUpperCase();
  let role = isFirst ? "admin" : "user";
  if (!isFirst) {
    if (invite) {
      const inviteErr = await checkInvite(env, invite);
      if (inviteErr) return fail(inviteErr);
      role = "vip";
    } else if (env.REQUIRE_INVITE === "1") {
      return fail("本站需要邀请码才能注册，请向站点主人索取");
    }
  }

  const codeErr = await consumeCode(env, email, "register", code);
  if (codeErr) return fail(codeErr);
  if (!isFirst && invite) await consumeInvite(env, invite);

  const quota = ROLE_QUOTA[role] || DEFAULT_QUOTA;
  // VIP 与管理员注册即带「精修」能力位；之后由管理员在后台单独改这一列
  const canRefineInit = (role === "vip" || role === "admin") ? 1 : 0;
  const res = await env.DB.prepare(
    `INSERT INTO users (email, pass_hash, role, can_refine, quota_pages, created_at, quota_reset_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  ).bind(email, await hashPassword(password), role, canRefineInit, quota, now, now).run();

  const userId = res.meta.last_row_id;
  await audit(env, userId, userId, "register", email + (invite ? "（邀请码）" : ""), request);
  const token = await createSession(env, userId, request);
  return json({
    ok: true,
    user: { email, role },
    message: role === "vip" ? `邀请码有效，已为你开通 VIP（每月 ${quota} 页）` : `注册成功（每月 ${quota} 页）`,
  }, 200, { "set-cookie": sessionCookie(token, SESSION_TTL, env.COOKIE_DOMAIN) });
}

async function apiLogin(request, env, url) {
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const email = normEmail(body.email);
  const password = String(body.password || "");
  if (!isEmail(email) || !password) return fail("邮箱或密码不对", 401);

  const ip = request.headers.get("cf-connecting-ip") || "0";
  if (await tooMany(env, [
    ["rl:login:ip:" + ip, 20, 900],
    ["rl:login:" + ip + ":" + email, 8, 900],
  ])) return fail("尝试太多次，请 15 分钟后再试", 429);

  const user = await env.DB.prepare(
    "SELECT id, email, pass_hash, role, status, quota_pages, used_pages FROM users WHERE email = ?",
  ).bind(email).first();
  const passOk = user ? await verifyPassword(password, user.pass_hash) : false;
  if (!user || !passOk) return fail("邮箱或密码不对", 401);
  if (user.status !== "active") return fail("这个账号已被停用，请联系管理员", 403);

  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare("UPDATE users SET last_login_at = ? WHERE id = ?").bind(now, user.id).run();
  await audit(env, user.id, user.id, "login", "", request);
  const token = await createSession(env, user.id, request);
  return json({ ok: true, user: { email: user.email, role: user.role } }, 200, {
    "set-cookie": sessionCookie(token, SESSION_TTL, env.COOKIE_DOMAIN),
  });
}

async function apiLogout(request, env) {
  const token = parseCookies(request)[SESSION_COOKIE];
  if (token) {
    await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(await sha256Hex(token)).run();
  }
  return json({ ok: true }, 200, { "set-cookie": sessionCookie("", 0, env.COOKIE_DOMAIN) });
}

async function apiResetPassword(request, env) {
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const email = normEmail(body.email);
  const code = String(body.code || "").trim();
  const password = String(body.password || "");
  if (!isEmail(email)) return fail("邮箱格式不对");
  if (password.length < MIN_PASSWORD) return fail(`密码至少 ${MIN_PASSWORD} 位`);

  const ip = request.headers.get("cf-connecting-ip") || "0";
  if (await tooMany(env, [["rl:reset:ip:" + ip, 10, 3600]])) return fail("操作太频繁", 429);

  const user = await env.DB.prepare("SELECT id FROM users WHERE email = ?").bind(email).first();
  if (!user) return fail("验证码不对或已过期");
  const codeErr = await consumeCode(env, email, "reset", code);
  if (codeErr) return fail(codeErr);

  await env.DB.prepare("UPDATE users SET pass_hash = ? WHERE id = ?")
    .bind(await hashPassword(password), user.id).run();
  // 改密码后把该用户所有会话踢掉（防止旧会话被盗用）
  await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id).run();
  await audit(env, user.id, user.id, "reset-password", "", request);
  return ok({ message: "密码已重置，请用新密码登录" });
}

async function apiChangePassword(request, env) {
  const user = await currentUser(env, request);
  if (!user) return fail("请先登录", 401);
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const oldPass = String(body.old_password || "");
  const newPass = String(body.new_password || "");
  if (newPass.length < MIN_PASSWORD) return fail(`新密码至少 ${MIN_PASSWORD} 位`);

  const row = await env.DB.prepare("SELECT pass_hash FROM users WHERE id = ?").bind(user.id).first();
  if (!(await verifyPassword(oldPass, row.pass_hash))) return fail("原密码不对", 401);

  await env.DB.prepare("UPDATE users SET pass_hash = ? WHERE id = ?")
    .bind(await hashPassword(newPass), user.id).run();
  await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id).run();
  await audit(env, user.id, user.id, "change-password", "", request);
  return json({ ok: true, message: "密码已修改，请重新登录" }, 200, { "set-cookie": sessionCookie("", 0, env.COOKIE_DOMAIN) });
}

async function apiMe(request, env) {
  const user = await currentUser(env, request);
  if (!user) return fail("未登录", 401);
  const used = await env.DB.prepare(
    "SELECT COUNT(*) AS n, COALESCE(SUM(pages),0) AS pages FROM usage WHERE user_id = ?",
  ).bind(user.id).first().catch(() => ({ n: 0, pages: 0 }));
  return ok({
    user: {
      email: user.email,
      role: user.role,
      can_refine: Number(user.can_refine) === 1 ? 1 : 0,
      quota_pages: user.quota_pages,
      used_pages: user.used_pages,
      created_at: user.created_at,
    },
    usage_total: { jobs: used.n || 0, pages: used.pages || 0 },
  });
}

async function apiUsage(request, env) {
  const user = await currentUser(env, request);
  if (!user) return fail("请先登录", 401);
  const list = await env.DB.prepare(
    "SELECT job_id, kind, pages, tokens_in, tokens_out, note, created_at FROM usage WHERE user_id = ? ORDER BY id DESC LIMIT 50",
  ).bind(user.id).all();
  return ok({ items: list.results || [] });
}

// ── 后台：总览统计 ─────────────────────────────────────────────────────
//
// 统计口径说明（都按北京时间 UTC+8 的自然日/自然月，避免"月初数据跑上个月"）：
//   今天零点 = floor((now + 8h) / 86400) * 86400 - 8h
const CST_OFFSET = 8 * 3600;
const DAY = 86400;

/** 北京时间某一天的零点（unix 秒） */
function cstDayStart(now) {
  return Math.floor((now + CST_OFFSET) / DAY) * DAY - CST_OFFSET;
}

/** 北京时间本月的零点（unix 秒） */
function cstMonthStart(now) {
  const d = new Date((now + CST_OFFSET) * 1000);
  return Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 1000) - CST_OFFSET;
}

/** 北京时间的 YYYY-MM-DD */
function cstDayKey(sec) {
  return new Date((sec + CST_OFFSET) * 1000).toISOString().slice(0, 10);
}

const STATS_DAYS = 14;

/** 管理员：总览可视化数据（一次把首页要的数字都取回来） */
async function apiAdminStats(request, env) {
  const actor = await currentUser(env, request);
  if (!actor || actor.role !== "admin") return fail("需要管理员权限", 403);

  const now = Math.floor(Date.now() / 1000);
  const dayStart = cstDayStart(now);
  const monthStart = cstMonthStart(now);
  const weekAgo = now - 7 * DAY;
  const chartFrom = dayStart - (STATS_DAYS - 1) * DAY;

  const q = (sql, ...args) => env.DB.prepare(sql).bind(...args).first();
  const [usersTotal, usersNew7d, monthAgg, allAgg, inviteAgg, dailyRes, topRes] = await Promise.all([
    q("SELECT COUNT(*) AS n FROM users"),
    q("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", weekAgo),
    q("SELECT COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs FROM usage WHERE created_at >= ?", monthStart),
    q("SELECT COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs FROM usage"),
    q("SELECT COALESCE(SUM(used_count),0) AS uses, COUNT(*) AS total FROM invites"),
    env.DB.prepare(
      `SELECT strftime('%Y-%m-%d', created_at + ?, 'unixepoch') AS day,
              COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs
         FROM usage WHERE created_at >= ? GROUP BY day ORDER BY day ASC`,
    ).bind(CST_OFFSET, chartFrom).all(),
    env.DB.prepare(
      `SELECT id, email, role, quota_pages, used_pages FROM users
        ORDER BY used_pages DESC, id ASC LIMIT 5`,
    ).all(),
  ]);

  // 补齐没有记录的日子，前端不用再判断缺口
  const byDay = {};
  for (const row of dailyRes.results || []) byDay[row.day] = row;
  const daily = [];
  for (let i = STATS_DAYS - 1; i >= 0; i -= 1) {
    const key = cstDayKey(dayStart - i * DAY);
    const hit = byDay[key];
    daily.push({ day: key, pages: hit ? Number(hit.pages) || 0 : 0, jobs: hit ? Number(hit.jobs) || 0 : 0 });
  }

  const topUsers = (topRes.results || []).map((u) => {
    const quota = Number(u.quota_pages) || 0;
    const used = Number(u.used_pages) || 0;
    return {
      id: u.id, email: u.email, role: u.role, quota_pages: quota, used_pages: used,
      // quota 为 0 表示不限量，没有百分比可算
      percent: quota > 0 ? Math.min(100, Math.round((used / quota) * 1000) / 10) : null,
    };
  });

  return ok({
    users_total: Number(usersTotal && usersTotal.n) || 0,
    users_new_7d: Number(usersNew7d && usersNew7d.n) || 0,
    month_pages: Number(monthAgg && monthAgg.pages) || 0,
    month_jobs: Number(monthAgg && monthAgg.jobs) || 0,
    total_pages: Number(allAgg && allAgg.pages) || 0,
    total_jobs: Number(allAgg && allAgg.jobs) || 0,
    invite_uses: Number(inviteAgg && inviteAgg.uses) || 0,
    invite_total: Number(inviteAgg && inviteAgg.total) || 0,
    daily,
    top_users: topUsers,
    month_start: monthStart,
    generated_at: now,
  });
}

// ── 后台：用户列表（搜索 / 排序 / 分页）────────────────────────────────

/** 排序白名单：只允许这些列名进入 SQL（值本身仍然走 bind） */
const USER_SORTS = {
  created_at: "created_at",
  last_login_at: "last_login_at",
  used_pages: "used_pages",
  quota_pages: "quota_pages",
  email: "email",
};
const MAX_PAGE_SIZE = 100;

/** 管理员：用户列表（q 模糊搜邮箱、sort 排序、order 升降序、page 分页） */
async function apiAdminUsers(request, env, url) {
  const user = await currentUser(env, request);
  if (!user || user.role !== "admin") return fail("需要管理员权限", 403);

  const qRaw = String(url.searchParams.get("q") || "").trim().slice(0, 80);
  const sortKey = USER_SORTS[String(url.searchParams.get("sort") || "")] ? String(url.searchParams.get("sort")) : "created_at";
  const order = String(url.searchParams.get("order") || "").toLowerCase() === "asc" ? "ASC" : "DESC";
  const pageSize = Math.min(Math.max(Math.trunc(Number(url.searchParams.get("page_size")) || 20), 1), MAX_PAGE_SIZE);
  const page = Math.max(Math.trunc(Number(url.searchParams.get("page")) || 1), 1);
  const offset = (page - 1) * pageSize;

  // LIKE 的通配符要转义，否则用户输入 % 就会变成"匹配所有"
  const like = qRaw ? "%" + qRaw.replace(/[\\%_]/g, (c) => "\\" + c) + "%" : "";
  const where = qRaw ? "WHERE email LIKE ? ESCAPE '\\'" : "";

  const listSql =
    `SELECT id, email, role, status, can_refine, quota_pages, used_pages, created_at, last_login_at
       FROM users ${where} ORDER BY ${USER_SORTS[sortKey]} ${order}, id DESC LIMIT ? OFFSET ?`;
  const countSql = `SELECT COUNT(*) AS n FROM users ${where}`;

  const listStmt = env.DB.prepare(listSql);
  const countStmt = env.DB.prepare(countSql);
  const [list, countRow] = await Promise.all([
    (qRaw ? listStmt.bind(like, pageSize, offset) : listStmt.bind(pageSize, offset)).all(),
    (qRaw ? countStmt.bind(like) : countStmt).first(),
  ]);

  const total = Number(countRow && countRow.n) || 0;
  return ok({
    users: list.results || [],
    total,
    page,
    page_size: pageSize,
    pages: Math.max(1, Math.ceil(total / pageSize)),
    q: qRaw,
    sort: sortKey,
    order: order.toLowerCase(),
  });
}

async function apiAdminUpdate(request, env) {
  const actor = await currentUser(env, request);
  if (!actor || actor.role !== "admin") return fail("需要管理员权限", 403);
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const id = Number(body.user_id);
  if (!id) return fail("缺少 user_id");
  // 自锁保护：管理员不能把当前登录的自己封掉（前端也会禁用这个按钮，这里是第二道闸）
  if (body.status === "banned" && id === Number(actor.id)) {
    return fail("不能封禁你自己当前登录的管理员账号");
  }

  const sets = [];
  const vals = [];
  if (["user", "vip", "admin"].includes(body.role)) {
    sets.push("role = ?");
    vals.push(body.role);
  }
  if (["active", "banned"].includes(body.status)) {
    sets.push("status = ?");
    vals.push(body.status);
  }
  const quotaGiven = Number.isFinite(Number(body.quota_pages)) && Number(body.quota_pages) >= 0;
  if (quotaGiven) {
    sets.push("quota_pages = ?");
    vals.push(Number(body.quota_pages));
  } else if (["user", "vip", "admin"].includes(body.role)) {
    // 只改角色没给额度 → 自动套用该角色的默认额度（30 / 50 / 200）
    sets.push("quota_pages = ?");
    vals.push(ROLE_QUOTA[body.role]);
  }
  if (Number.isFinite(Number(body.reset_used)) && Number(body.reset_used) === 1) {
    sets.push("used_pages = 0");
  }
  // 精修能力位：只在显式传 0/1 时改。
  // 不跟着 role 自动变 —— 角色只是注册时的默认值，管理员可能想让某个 VIP 不用精修
  // （或给某个普通用户体验一下），一改角色就覆盖掉他的设置反而更意外。
  const refineGiven = Number(body.can_refine);
  if (Number.isFinite(refineGiven) && (refineGiven === 0 || refineGiven === 1)) {
    sets.push("can_refine = ?");
    vals.push(refineGiven);
  }
  if (!sets.length) return fail("没有要改的字段");

  vals.push(id);
  await env.DB.prepare(`UPDATE users SET ${sets.join(", ")} WHERE id = ?`).bind(...vals).run();
  if (body.status === "banned") {
    await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(id).run();
  }
  await audit(env, actor.id, id, "admin-update", JSON.stringify(body).slice(0, 200), request);
  // 回传更新后的这一行，前端可以就地刷新（不用整表重载）
  const updated = await env.DB.prepare(
    "SELECT id, email, role, status, can_refine, quota_pages, used_pages, created_at, last_login_at FROM users WHERE id = ?",
  ).bind(id).first();
  return ok({ message: "已更新", user: updated || null });
}

/** 管理员：生成邀请码 */
async function apiAdminInvite(request, env) {
  const actor = await currentUser(env, request);
  if (!actor || actor.role !== "admin") return fail("需要管理员权限", 403);
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");

  const count = Math.min(Math.max(Number(body.count) || 1, 1), 20);
  const maxUses = Math.min(Math.max(Number(body.max_uses) || 1, 1), 100);
  const days = Math.min(Math.max(Number(body.days) || 30, 1), 3650);
  const now = Math.floor(Date.now() / 1000);
  const codes = [];
  for (let i = 0; i < count; i += 1) {
    const code = newInviteCode();
    await env.DB.prepare(
      "INSERT INTO invites (code, created_by, max_uses, used_count, expires_at, created_at) VALUES (?, ?, ?, 0, ?, ?)",
    ).bind(code, actor.id, maxUses, now + days * 86400, now).run();
    codes.push(code);
  }
  await audit(env, actor.id, null, "invite-create", `${count} 个 × ${maxUses} 次`, request);
  return ok({ codes, max_uses: maxUses, days });
}

/** 管理员：邀请码列表 */
async function apiAdminInvites(request, env) {
  const actor = await currentUser(env, request);
  if (!actor || actor.role !== "admin") return fail("需要管理员权限", 403);
  const list = await env.DB.prepare(
    `SELECT code, max_uses, used_count, expires_at, created_at FROM invites
      ORDER BY created_at DESC, code DESC LIMIT 100`,
  ).all();
  return ok({ invites: list.results || [] });
}

// ── 后台：通道状态 + 余额（尽力而为，绝不让余额拖垮整个接口）──────────────

const CHANNEL_TIMEOUT = 8000; // 每个通道每个请求 3 秒硬超时
const shortErr = (e) => String((e && e.message) || e || "未知错误").slice(0, 160);

/** 3 秒超时用的 AbortController 包装 */
function withTimeout(ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => { try { ctrl.abort(); } catch (err) { /* 已结束 */ } }, ms);
  return { signal: ctrl.signal, done: () => clearTimeout(timer) };
}

/** 从各种形状的余额响应里找一个看得懂的字段；找不到返回 null */
function pickBalance(root) {
  const buckets = [];
  const push = (o) => { if (o && typeof o === "object") buckets.push(o); };
  push(root);
  if (root && typeof root === "object") { push(root.data); push(root.result); push(root.info); }
  const keys = ["balance", "total_balance", "totalBalance", "available_balance", "availableBalance",
                "remaining", "remain", "credit", "credits", "total_credits"];
  for (const o of buckets) {
    for (const k of keys) {
      const v = o[k];
      if (typeof v === "number" && Number.isFinite(v)) return String(v);
      if (typeof v === "string" && v.trim()) return v.trim().slice(0, 40);
    }
  }
  return null;
}

/**
 * 连通性实测。
 *
 * ★ 两种协议都要试：Codex 类中转站（如 codex.water555.com）只支持
 *   /responses，打 /chat/completions 会回 405 —— 线上就因为这个把它误判成"不通"。
 */
async function probeChannelChat(baseUrl, apiKey, model) {
  const first = await probeOnce(baseUrl + "/chat/completions", apiKey, model, "chat");
  if (first.ok) return first;
  // 405/404 说明这个网关不认 chat/completions，换 Responses 协议再试一次
  if (/HTTP (404|405)/.test(first.error || "")) {
    const second = await probeOnce(baseUrl + "/responses", apiKey, model, "responses");
    if (second.ok) return second;
    return second;
  }
  return first;
}

/** 单次探测（chat 或 responses 协议） */
async function probeOnce(url, apiKey, model, kind) {
  const t = withTimeout(CHANNEL_TIMEOUT);
  const started = Date.now();
  try {
    const body = kind === "responses"
      ? { model, input: '说"ok"', max_output_tokens: 16 }
      : { model, messages: [{ role: "user", content: '说"ok"' }], max_tokens: 4 };
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(apiKey ? { authorization: "Bearer " + apiKey } : {}),
      },
      body: JSON.stringify(body),
      signal: t.signal,
    });
    const text = (await resp.text()).slice(0, 800);
    const ms = Date.now() - started;
    if (!resp.ok) {
      const hint = /unsupported_country_region_territory/.test(text)
        ? "（该服务不支持当前地区 —— 从 Cloudflare 发请求用不了这个通道）"
        : "";
      return { ok: false, ms, error: "HTTP " + resp.status + "：" + text.slice(0, 160) + hint };
    }
    // 有的网关用 200 回一个 error 体，这种也算不通
    try {
      const data = JSON.parse(text);
      if (data && data.error) return { ok: false, ms, error: "接口返回错误：" + String(data.error.message || data.error).slice(0, 200) };
    } catch (err) { /* 不是 JSON 也无所谓，HTTP 200 就算通 */ }
    return { ok: true, ms, error: null };
  } catch (e) {
    return {
      ok: false,
      ms: Date.now() - started,
      error: e && e.name === "AbortError" ? "超过 " + CHANNEL_TIMEOUT / 1000 + " 秒没有响应" : shortErr(e),
    };
  } finally {
    t.done();
  }
}

/** 余额：能查就查，查不到只记原因（410/404/超时/非 JSON 都算"查不到"） */
async function probeChannelBalance(baseUrl, apiKey) {
  const t = withTimeout(CHANNEL_TIMEOUT);
  try {
    const resp = await fetch(baseUrl + "/user/info", {
      method: "GET",
      headers: { accept: "application/json", ...(apiKey ? { authorization: "Bearer " + apiKey } : {}) },
      signal: t.signal,
    });
    const text = (await resp.text()).slice(0, 2000);
    if (!resp.ok) {
      const note = resp.status === 410
        ? "该通道的余额接口已下线（HTTP 410 deprecated），查不到余额"
        : "该通道不提供余额接口（GET /user/info 返回 HTTP " + resp.status + "）";
      return { balance: null, note };
    }
    let data = null;
    try { data = JSON.parse(text); } catch (err) { data = null; }
    if (!data) return { balance: null, note: "余额接口返回的不是 JSON，无法解析" };
    const found = pickBalance(data);
    if (found === null) return { balance: null, note: "余额接口可用，但响应里没有可识别的余额字段" };
    return { balance: found, note: "来自 GET /user/info" };
  } catch (e) {
    const aborted = e && e.name === "AbortError";
    return { balance: null, note: aborted ? "余额查询超时（" + CHANNEL_TIMEOUT / 1000 + " 秒）" : "余额查询失败：" + shortErr(e) };
  } finally {
    t.done();
  }
}

/** 探测单个通道：连通性 + 余额并发跑，互不影响 */
async function probeChannel(code, profile) {
  const name = String(profile.name || code);
  const baseUrl = String(profile.baseUrl || "").trim().replace(/\/+$/, "");
  const model = String(profile.chatModel || profile.model || "");
  const apiKey = String(profile.apiKey || "");
  const base = { code, name, baseUrl, model, ok: false, ms: 0, error: null, balance: null, balance_note: "" };

  if (!baseUrl) {
    return { ...base, error: "通道没有配置 baseUrl", balance_note: "未探测（缺少 baseUrl）" };
  }
  if (!model) {
    const bal = await probeChannelBalance(baseUrl, apiKey);
    return { ...base, error: "通道没有配置 chatModel，无法发最小请求", balance: bal.balance, balance_note: bal.note };
  }

  const [chat, bal] = await Promise.allSettled([
    probeChannelChat(baseUrl, apiKey, model),
    probeChannelBalance(baseUrl, apiKey),
  ]);

  const chatVal = chat.status === "fulfilled"
    ? chat.value
    : { ok: false, ms: 0, error: "探测异常：" + shortErr(chat.reason) };
  const balVal = bal.status === "fulfilled"
    ? bal.value
    : { balance: null, note: "余额查询异常：" + shortErr(bal.reason) };

  return { ...base, ...chatVal, balance: balVal.balance, balance_note: balVal.note };
}

/** 管理员：通道状态 + 余额（读 Worker 密钥 PROFILES_JSON） */
async function apiAdminChannels(request, env) {
  const actor = await currentUser(env, request);
  if (!actor || actor.role !== "admin") return fail("需要管理员权限", 403);

  const raw = env.PROFILES_JSON;
  let profiles = null;
  if (raw) {
    try { profiles = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (err) { profiles = null; }
  }
  if (!profiles || typeof profiles !== "object" || Array.isArray(profiles)) {
    return ok({ channels: [], note: "服务端没有配置 PROFILES_JSON（或不是合法 JSON 对象），没有可探测的通道", generated_at: Math.floor(Date.now() / 1000) });
  }

  const entries = Object.entries(profiles).filter(([, v]) => v && typeof v === "object");
  if (!entries.length) {
    return ok({ channels: [], note: "PROFILES_JSON 里还没有配置任何通道", generated_at: Math.floor(Date.now() / 1000) });
  }

  // 通道之间并发探测；某个通道抛错也只影响它自己（allSettled，不整体失败）
  const settled = await Promise.allSettled(entries.map(([code, p]) => probeChannel(code, p)));
  const channels = settled.map((r, i) => {
    const [code, p] = entries[i];
    if (r.status === "fulfilled") return r.value;
    return {
      code, name: String(p.name || code), baseUrl: String(p.baseUrl || ""),
      model: String(p.chatModel || p.model || ""), ok: false, ms: 0,
      error: "探测异常：" + shortErr(r.reason), balance: null, balance_note: "未查询（探测本身失败）",
    };
  });

  return ok({ channels, generated_at: Math.floor(Date.now() / 1000) });
}

// ── 入口 ──────────────────────────────────────────────────────────────

const SECURITY_HEADERS = {
  "x-frame-options": "DENY",
  "x-content-type-options": "nosniff",
  "referrer-policy": "same-origin",
  "content-security-policy":
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    try {
      if (path === "/" || path === "/index.html") {
        return new Response(PAGE, {
          headers: { "content-type": "text/html; charset=utf-8", ...SECURITY_HEADERS },
        });
      }
      if (path === "/healthz") return ok({ service: "aurora-account" });

      // 所有写接口：POST + 同源
      if (path.startsWith("/api/")) {
        if (request.method !== "POST" && request.method !== "GET") return fail("方法不支持", 405);
        if (request.method === "POST" && !sameOrigin(request, url)) return fail("来源不合法", 403);
        switch (path) {
          case "/api/send-code": return await apiSendCode(request, env, url);
          case "/api/register": return await apiRegister(request, env, url);
          case "/api/login": return await apiLogin(request, env, url);
          case "/api/logout": return await apiLogout(request, env);
          case "/api/reset-password": return await apiResetPassword(request, env);
          case "/api/change-password": return await apiChangePassword(request, env);
          case "/api/me": return await apiMe(request, env);
          case "/api/usage": return await apiUsage(request, env);
          case "/api/admin/users": return await apiAdminUsers(request, env, url);
          case "/api/admin/stats": return await apiAdminStats(request, env);
          case "/api/admin/channels": return await apiAdminChannels(request, env);
          case "/api/admin/update": return await apiAdminUpdate(request, env);
          case "/api/admin/invite": return await apiAdminInvite(request, env);
          case "/api/admin/invites": return await apiAdminInvites(request, env);
          default: return fail("没有这个接口：" + path, 404);
        }
      }
      return fail("没有这个页面：" + path, 404);
    } catch (err) {
      return fail("服务器内部错误：" + (err && err.message ? err.message : String(err)), 500);
    }
  },
};
