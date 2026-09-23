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
  const res = await env.DB.prepare(
    `INSERT INTO users (email, pass_hash, role, quota_pages, created_at, quota_reset_at)
     VALUES (?, ?, ?, ?, ?, ?)`,
  ).bind(email, await hashPassword(password), role, quota, now, now).run();

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

/** 管理员：改权限 / 发额度 / 封号（阶段 3 的后台先留最小可用版本） */
async function apiAdminUsers(request, env) {
  const user = await currentUser(env, request);
  if (!user || user.role !== "admin") return fail("需要管理员权限", 403);
  const list = await env.DB.prepare(
    "SELECT id, email, role, status, quota_pages, used_pages, created_at, last_login_at FROM users ORDER BY id DESC LIMIT 200",
  ).all();
  return ok({ users: list.results || [] });
}

async function apiAdminUpdate(request, env) {
  const actor = await currentUser(env, request);
  if (!actor || actor.role !== "admin") return fail("需要管理员权限", 403);
  const body = await readJson(request);
  if (!body) return fail("请求格式不对");
  const id = Number(body.user_id);
  if (!id) return fail("缺少 user_id");

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
  if (!sets.length) return fail("没有要改的字段");

  vals.push(id);
  await env.DB.prepare(`UPDATE users SET ${sets.join(", ")} WHERE id = ?`).bind(...vals).run();
  if (body.status === "banned") {
    await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(id).run();
  }
  await audit(env, actor.id, id, "admin-update", JSON.stringify(body).slice(0, 200), request);
  return ok({ message: "已更新" });
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
          case "/api/admin/users": return await apiAdminUsers(request, env);
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
