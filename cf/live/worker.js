// 由 cf/live/build.py 从 live-interpreter/server.mjs 自动生成，请勿手改

// ════════════════════════════════════════════════════════════════════════
//  以下是 Cloudflare 适配层（自动生成，勿手改；改业务逻辑请改 server.mjs 后重跑 build.py）
// ════════════════════════════════════════════════════════════════════════
import { ASSETS, GLOSSARY_TEXT, ORAL_TEXT } from "./assets.js";
import { currentUser } from "../shared/session.js";

/** 每次请求开始前由 fetch 入口填进来（Worker 没有全局 env） */
let LIVE_ENV = {};
let LIVE_CTX = null;
let LIVE_USER = null;      // 本次请求的登录用户（用于计费）

/** 同传计费：累计音频秒数，每满 60 秒扣 1 页额度（写进账户系统的 usage 表） */
async function meterLive(env, userId, seconds) {
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare(
    `INSERT INTO live_meter (user_id, seconds, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(user_id) DO UPDATE SET seconds = seconds + ?, updated_at = ?`,
  ).bind(userId, seconds, now, seconds, now).run();
  const row = await env.DB.prepare("SELECT seconds FROM live_meter WHERE user_id = ?").bind(userId).first();
  const total = Number((row && row.seconds) || 0);
  const minutes = Math.floor(total / 60);
  if (minutes >= 1) {
    await env.DB.prepare("UPDATE live_meter SET seconds = seconds - ? WHERE user_id = ?")
      .bind(minutes * 60, userId).run();
    await env.DB.prepare(
      `INSERT INTO usage (user_id, kind, pages, note, created_at) VALUES (?, 'live', ?, ?, ?)`,
    ).bind(userId, minutes, "同声传译 " + minutes + " 分钟", now).run();
    await env.DB.prepare("UPDATE users SET used_pages = used_pages + ? WHERE id = ?")
      .bind(minutes, userId).run();
  }
}
const PE = (name, dflt = "") => (LIVE_ENV && LIVE_ENV[name] !== undefined ? LIVE_ENV[name] : dflt);

/** 让原代码里的 existsSync/readFileSync/writeFileSync 继续可用：
 *  profiles.json → 来自 Worker 密钥；glossary/oral → 来自内置资源；
 *  其它数据文件（usage.json 等）走内存，由 D1 负责持久化。 */
function existsSync(p) {
  const n = String(p).split("/").pop();
  if (n === "profiles.json") return !!PE("PROFILES_JSON");
  if (n === "glossary.json" || n === "oral.json") return true;
  return false;
}
function readFileSync(p, _enc) {
  const n = String(p).split("/").pop();
  if (n === "profiles.json") return PE("PROFILES_JSON") || "{}";
  if (n === "glossary.json") return GLOSSARY_TEXT;
  if (n === "oral.json") return ORAL_TEXT;
  return "";
}
function writeFileSync() { /* 状态由 D1 落库，这里兜住原代码的写文件调用 */ }
const nodeRequire = () => ({ existsSync, readFileSync, writeFileSync });
const fileURLToPath = () => "/live";
const http = { createServer: null };
const readFile = async (p) => {
  const name = String(p).split("/").pop();
  if (name === "profiles.json") return new TextEncoder().encode(PE("PROFILES_JSON") || "{}");
  if (name === "glossary.json") return new TextEncoder().encode(GLOSSARY_TEXT);
  if (name === "oral.json") return new TextEncoder().encode(ORAL_TEXT);
  const asset = ASSETS[name];
  if (asset === undefined) throw Object.assign(new Error("not found"), { code: "ENOENT" });
  return new TextEncoder().encode(asset);
};
const path = {
  dirname: (p) => String(p).replace(/\/[^/]*$/, "") || "/",
  join: (...xs) => xs.filter(Boolean).join("/").replace(/\/{2,}/g, "/"),
  extname: (p) => { const m = /\.[a-z0-9]+$/i.exec(String(p)); return m ? m[0] : ""; },
  basename: (p) => String(p).split("/").pop(),
  resolve: (...xs) => xs.join("/"),
};

/** 备用 MIME 表（原版自带一份 MIME，这里只在缺失时兜底） */
const LIVE_MIME_FALLBACK = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
  ".wav": "audio/wav", ".txt": "text/plain; charset=utf-8",
};
// ════════════════════════════════════════════════════════════════════════

// LiveBridge 同声传译 · 零依赖后端（Node >= 18）
// 职责：托管静态前端 + 统一代理 OpenAI 兼容 API（避免浏览器跨域/密钥直曝），
//       MOCKF()=1 时进入离线演示模式（模拟翻译 / 提示音 TTS / 假识别结果）。

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(ROOT, 'public');
const PORT = Number(PE('PORT') || 8787);
const HOST = (PE('HOST') || '').trim() || undefined;  // 默认监听所有网卡；反代部署可设 127.0.0.1
function MOCKF() { return /^(1|true|yes)$/i.test(PE('MOCK', '') || ''); }
const MAX_AUDIO_BYTES = 40 * 1024 * 1024;

function ENVF() {
  return {
    baseUrl: String(PE('AI_BASE_URL', '')).trim().replace(/\/+$/, ''),
    apiKey: String(PE('AI_API_KEY', '')).trim(),
    chatModel: String(PE('CHAT_MODEL', 'deepseek-chat')).trim(),
    ttsModel: String(PE('TTS_MODEL', 'tts-1')).trim(),
    ttsVoice: String(PE('TTS_VOICE', 'nova')).trim(),
    sttModel: String(PE('STT_MODEL', 'whisper-1')).trim(),
  };
}

// ---------- API 预设（Profile）：管理员在 profiles.json 预置配置，使用者只填“代号” ----------
// profiles.json 结构：{ "代号": { name, note, baseUrl, apiKey, chatModel, sttModel, ttsModel, ttsVoice } }
function loadProfilesSync() {
  const file = PE('PROFILES_FILE') || path.join(ROOT, 'profiles.json');
  try {
    if (!existsSync(file)) return {};
    const data = JSON.parse(readFileSync(file, 'utf8'));
    const out = {};
    for (const [code, p] of Object.entries(data || {})) {
      if (p && typeof p === 'object' && code) {
        out[code] = {
          name: String(p.name || code).slice(0, 60),
          note: String(p.note || '').slice(0, 200),
          baseUrl: String(p.baseUrl || '').trim().replace(/\/+$/, ''),
          apiKey: String(p.apiKey || '').trim(),
          chatModel: String(p.chatModel || '').trim(),
          sttModel: String(p.sttModel || '').trim(),
          ttsModel: String(p.ttsModel || '').trim(),
          ttsVoice: String(p.ttsVoice || '').trim(),
          sttBaseUrl: String(p.sttBaseUrl || '').trim().replace(/\/+$/, ''),
          sttApi: String(p.sttApi || '').trim(),   // 'openai'(默认) 或 'dashscope'(阿里云百炼)
          fallbackSttApi: String(p.fallbackSttApi || '').trim(),
          fallbackSttBaseUrl: String(p.fallbackSttBaseUrl || '').trim().replace(/\/+$/, ''),
          fallbackSttApiKey: String(p.fallbackSttApiKey || '').trim(),
          fallbackSttModel: String(p.fallbackSttModel || '').trim(),
          sttApiKey: String(p.sttApiKey || '').trim(),
          // 定稿精修可另走一家（如 GPT 中转站，OpenAI Responses 协议）
          refineBaseUrl: String(p.refineBaseUrl || '').trim().replace(/\/+$/, ''),
          refineApiKey: String(p.refineApiKey || '').trim(),
          refineModel: String(p.refineModel || '').trim(),
          refineApi: String(p.refineApi || '').trim(),
        };
      }
    }
    return out;
  } catch (e) {
    console.warn('[profiles] 加载失败：', e.message);
    return {};
  }
}
let __profilesCache = null;
function PROFILESF() { if (!__profilesCache) __profilesCache = loadProfilesSync(); return __profilesCache; }

// ---------- 术语表：翻译时按需注入（只注入原文中出现的术语，避免提示词膨胀） ----------
function loadGlossary() {
  const file = PE('GLOSSARY_FILE') || path.join(ROOT, 'glossary.json');
  const out = {};
  try {
    if (!existsSync(file)) return out;
    const data = JSON.parse(readFileSync(file, 'utf8'));
    const walk = (o) => {
      for (const [k, v] of Object.entries(o || {})) {
        if (typeof v === 'string') out[k.toLowerCase()] = k + '→' + v;
        else if (v && typeof v === 'object') walk(v);
      }
    };
    walk(data);
  } catch (e) { console.warn('[glossary] 加载失败：', e.message); }
  return out;
}
const GLOSSARY = loadGlossary();
// ---------- 用量统计与限额（防刷爆 / 控成本） ----------
const LIMITS = {
  enabled: !/^(0|false|no)$/i.test(PE('LIMIT_ENABLED') || '1'),
  perIpSttSec: Number(PE('LIMIT_PER_IP_STT_HOURS') || 4) * 3600,      // 每个 IP 每天可识别的音频时长
  perIpTrCalls: Number(PE('LIMIT_PER_IP_TRANSLATE_CALLS') || 30000),  // 每个 IP 每天翻译调用上限
  globalSttSec: Number(PE('LIMIT_GLOBAL_STT_HOURS') || 40) * 3600,    // 全站每天识别音频上限
  globalTrCalls: Number(PE('LIMIT_GLOBAL_TRANSLATE_CALLS') || 300000),// 全站每天翻译调用上限
};
const USAGE_FILE = PE('USAGE_FILE') || path.join(ROOT, 'usage.json');
// 成本单价（元；可用环境变量覆盖）：识别按音频小时，翻译按百万 token
const PRICE = {
  sttPerHour: Number(PE('PRICE_STT_PER_HOUR') || 0.6),
  inPerM: Number(PE('PRICE_IN_PER_M') || 1.0),
  outPerM: Number(PE('PRICE_OUT_PER_M') || 2.0),
};
function estimateCost(u) {
  const stt = ((u.sttSec || 0) / 3600) * PRICE.sttPerHour;
  const tin = (u.charsIn || 0) / 4;          // 英文约 4 字符/token
  const tout = (u.charsOut || 0) / 1.6;      // 中文约 1.6 字符/token
  const tr = (tin / 1e6) * PRICE.inPerM + (tout / 1e6) * PRICE.outPerM;
  return { stt: +stt.toFixed(3), translate: +tr.toFixed(3), total: +(stt + tr).toFixed(3) };
}
const todayStr = () => new Date().toISOString().slice(0, 10);
let USAGE = { day: todayStr(), total: { sttSec: 0, sttCalls: 0, trCalls: 0, ttsCalls: 0 }, perIp: {} };
try { if (existsSync(USAGE_FILE)) { const u = JSON.parse(readFileSync(USAGE_FILE, 'utf8')); if (u && u.day === todayStr()) USAGE = u; } } catch {}
let usageDirty = false;
// （Cloudflare 版）用量改为实例内存保存，不做定时落盘

function clientIp(req) {
  const xff = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  return xff || (req.socket && req.socket.remoteAddress) || 'unknown';
}
function resetUsageIfNewDay() {
  if (USAGE.day !== todayStr()) USAGE = { day: todayStr(), total: { sttSec: 0, sttCalls: 0, trCalls: 0, ttsCalls: 0 }, perIp: {} };
}
function bumpUsage(req, kind, seconds) {
  resetUsageIfNewDay();
  const ip = clientIp(req);
  const u = USAGE.perIp[ip] || (USAGE.perIp[ip] = { sttSec: 0, sttCalls: 0, trCalls: 0, ttsCalls: 0 });
  const inc = (o) => { if (kind === 'stt') { o.sttCalls += 1; o.sttSec += (seconds || 0); } else if (kind === 'tr') o.trCalls += 1; else o.ttsCalls += 1; };
  inc(u); inc(USAGE.total);
  capMap(USAGE.perIp, 500);   // 访客表上限
  usageDirty = true;
}
function overLimit(req, kind, seconds) {
  if (!LIMITS.enabled) return null;
  resetUsageIfNewDay();
  const ip = clientIp(req);
  const u = USAGE.perIp[ip] || { sttSec: 0, trCalls: 0 };
  if (kind === 'stt') {
    if (u.sttSec + (seconds || 0) > LIMITS.perIpSttSec) return '今日识别额度已用完（每人每天 ' + (LIMITS.perIpSttSec / 3600) + ' 小时），请联系管理员';
    if (USAGE.total.sttSec + (seconds || 0) > LIMITS.globalSttSec) return '今日全站识别额度已用完，请联系管理员';
  } else if (kind === 'tr') {
    if (u.trCalls + 1 > LIMITS.perIpTrCalls) return '今日翻译额度已用完，请联系管理员';
    if (USAGE.total.trCalls + 1 > LIMITS.globalTrCalls) return '今日全站翻译额度已用完，请联系管理员';
  }
  return null;
}
function estAudioSeconds(len, contentType) {
  const ct = String(contentType || '');
  if (ct.includes('wav')) return Math.max(0, (len - 44) / 32000);   // 16k 单声道 16bit
  return len / 4000;                                              // 压缩格式粗估
}

// ---------- 管理员面板：错误日志环形缓冲 ----------
const LOG_RING = [];
const _origWarn = console.warn.bind(console);
const _origErr = console.error.bind(console);
function pushLog(level, args) {
  try {
    LOG_RING.push({ t: Date.now(), level, msg: args.map((a) => (typeof a === 'string' ? a : (a && a.message) || JSON.stringify(a))).join(' ').slice(0, 400) });
    if (LOG_RING.length > 300) LOG_RING.shift();
  } catch {}
}
console.warn = (...a) => { pushLog('warn', a); _origWarn(...a); };
console.error = (...a) => { pushLog('error', a); _origErr(...a); };
function adminAuth(req, u) {
  const tok = PE('ADMIN_TOKEN') || '';
  if (!tok) return true;
  return u.searchParams.get('token') === tok || String(req.headers['x-admin-token'] || '') === tok;
}

// 统一附加要求：禁止用省略号/等等敷衍（ASR 半截话时模型爱这么干）
const NO_ELLIPSIS = ' 硬性要求：不要使用省略号（……、...）或“等等”来省略内容；即使原文不完整或是半句话，也要把已有的内容完整翻译出来。';

// ---------- 领域/风格提示：让译文符合场景（技术课、商务会、医学…） ----------
const DOMAIN_PROMPTS = {
  auto: '',
  tech: '场景：计算机/软件技术课程或技术会议。术语必须使用业界通用中文译法（如 compiler→编译器、pointer→指针），代码/命令/函数名/产品名保持原文不译。',
  business: '场景：商务谈判/公司会议。语气正式得体，敬语与商业术语符合中文商务习惯。',
  academic: '场景：学术讲座/论文汇报。用词准确严谨，保持学术文风，公式与变量名保持原文。',
  medical: '场景：医学/生物医学。术语使用规范医学术语，不得臆造，剂量与数值务必准确。',
  legal: '场景：法律/合同。用语严谨，法律术语准确，不得含糊或意译关键条款。',
  class: '场景：课堂教学。译文口语化、清晰易懂，便于学生即时理解，专业词首次出现可保留英文原词。',
};
function domainHint(domain, notes) {
  const d = DOMAIN_PROMPTS[String(domain || 'auto')] || '';
  const n = String(notes || '').trim().slice(0, 300);
  let s = d ? (' ' + d) : '';
  if (n) s += ' 额外要求：' + n;
  return s;
}
// 字符统计（用于成本估算）
function bumpChars(req, cin, cout) {
  if (!cin && !cout) return;
  resetUsageIfNewDay();
  const ip = clientIp(req);
  const u = USAGE.perIp[ip] || (USAGE.perIp[ip] = { sttSec: 0, sttCalls: 0, trCalls: 0, ttsCalls: 0, charsIn: 0, charsOut: 0 });
  u.charsIn = (u.charsIn || 0) + (cin || 0); u.charsOut = (u.charsOut || 0) + (cout || 0);
  USAGE.total.charsIn = (USAGE.total.charsIn || 0) + (cin || 0);
  USAGE.total.charsOut = (USAGE.total.charsOut || 0) + (cout || 0);
  usageDirty = true;
}

// ---------- 自动挖词：从真实对话里学习专有术语，自动补进词表 ----------
const AUTO_GLOSSARY = !/^(0|false|no)$/i.test(PE('AUTO_GLOSSARY') || '1');
const MINED_FILE = PE('MINED_FILE') || path.join(ROOT, 'mined.json');
const STOP = new Set(['The','This','That','These','Those','There','Then','They','What','When','Where','Which','Who','Why','How','You','Your','We','Our','It','Its','He','She','His','Her','And','But','Or','So','If','In','On','At','For','With','As','To','Of','A','An','Is','Are','Was','Were','Be','Do','Does','Did','Can','Could','Should','Would','Will','May','Might','Must','Not','No','Yes','Hello','Okay','OK','Ok','Please','Thank','Thanks','Today','Tomorrow','Yesterday','Now','First','Second','Third','One','Two','Three','Also','Because','However','Actually','Really','Just','Like','Well','Let','I','US','IT','TV','PM','AM','MR','MS','PDF','URL','CPU','OK']);
let MINED = {};
try { if (existsSync(MINED_FILE)) MINED = JSON.parse(readFileSync(MINED_FILE, 'utf8')) || {}; } catch {}
for (const [k, v] of Object.entries(MINED)) { if (v && v.zh) GLOSSARY[k.toLowerCase()] = k + '→' + v.zh; }
const TERM_COUNT = {};
let LAST_CFG = null; let MINE_BUSY = false; let RECENT_N = 0;
function recordSource(text) {
  if (!AUTO_GLOSSARY || !text) return;
  const t = String(text);
  const re = /\b([A-Z][A-Za-z0-9+#.\-]{1,20}|[A-Z]{2,6})\b/g;
  let m;
  while ((m = re.exec(t))) {
    const w = m[0];
    if (STOP.has(w) || w.length < 2) continue;
    const before = t.slice(Math.max(0, m.index - 2), m.index);
    const midSentence = /[a-z0-9,;:]\s$/.test(before);
    const e = TERM_COUNT[w] || (TERM_COUNT[w] = { n: 0, mid: 0 });
    e.n += 1; if (midSentence) e.mid += 1;
  }
  capMap(TERM_COUNT, 3000);   // 术语候选表上限
  RECENT_N += 1;
  if (RECENT_N % Number(PE('MINE_EVERY') || 12) === 0) mineTerms();
}
function saveMined() {
  try { writeFileSync(MINED_FILE, JSON.stringify(MINED, null, 2)); } catch (e) { console.warn('[mined] 保存失败', e.message); }
}
async function mineTerms() {
  if (MINE_BUSY || !AUTO_GLOSSARY || !LAST_CFG) return;
  const cands = Object.entries(TERM_COUNT)
    .filter(([k, v]) => v.n >= 3 && v.mid >= 1 && !GLOSSARY[k.toLowerCase()] && !MINED[k] && !STOP.has(k))
    .sort((a, b) => b[1].n - a[1].n).slice(0, 20).map(([k]) => k);
  if (!cands.length) return;
  MINE_BUSY = true;
  try {
    const msgs = [
      { role: 'system', content: '你是技术术语翻译助手。只输出一个 JSON 对象：键是给定的英文术语，值是它在计算机/技术语境下的标准中文译法（若确无通行译法则输出原词）。不要输出任何解释或代码块标记。' },
      { role: 'user', content: JSON.stringify(cands) },
    ];
    const out = await callChatOnce({ baseUrl: LAST_CFG.baseUrl, apiKey: LAST_CFG.apiKey, model: LAST_CFG.chatModel, messages: msgs });
    const json = String(out).replace(/^[^{]*/, '').replace(/[^}]*$/, '');
    const map = JSON.parse(json);
    let added = 0;
    for (const [k, v] of Object.entries(map)) {
      if (typeof v !== 'string' || !v.trim()) continue;
      MINED[k] = { zh: v.trim(), n: (TERM_COUNT[k] && TERM_COUNT[k].n) || 1, auto: true };
      GLOSSARY[k.toLowerCase()] = k + '→' + v.trim();
      added += 1;
    }
    if (added) {
      capMap(MINED, 600);   // 自动术语上限
      saveMined();
      console.log('[mined] 自动收录术语', added, '条，累计', Object.keys(MINED).length);
    }
  } catch (e) { console.warn('[mined] 挖掘失败：', e.message); }
  finally { MINE_BUSY = false; }
}
function capMap(obj, max) {   // 限制内存表规模，防止长时间运行 OOM
  const keys = Object.keys(obj);
  if (keys.length <= max) return obj;
  for (const k of keys.slice(0, keys.length - max)) delete obj[k];
  return obj;
}
// ---------- 口语库：口语填充词及其处理方式（与术语库同样支持自动挖掘） ----------
function loadOral() {
  const file = PE('ORAL_FILE') || path.join(ROOT, 'oral.json');
  const out = {};
  try {
    if (!existsSync(file)) return out;
    const data = JSON.parse(readFileSync(file, 'utf8'));
    const walk = (o) => {
      for (const [k, v] of Object.entries(o || {})) {
        if (k.startsWith('_')) continue;   // 跳过 _说明 之类的注释键
        if (typeof v === 'string') out[k.toLowerCase()] = { term: k, val: v };
        else if (v && typeof v === 'object') walk(v);
      }
    };
    walk(data);
  } catch (e) { console.warn('[oral] 加载失败：', e.message); }
  return out;
}
const ORAL = loadOral();
const ORAL_FIXED_N = Object.keys(ORAL).length;
const ORAL_MINED_FILE = PE('ORAL_MINED_FILE') || path.join(ROOT, 'oral_mined.json');
let ORAL_MINED = {};
try { if (existsSync(ORAL_MINED_FILE)) ORAL_MINED = JSON.parse(readFileSync(ORAL_MINED_FILE, 'utf8')) || {}; } catch {}
for (const [k, v] of Object.entries(ORAL_MINED)) { if (v && v.val) ORAL[k.toLowerCase()] = { term: k, val: v.val, auto: true }; }
const ORAL_COUNT = {};   // 命中计数
const ORAL_CAND = {};    // 自动挖掘候选（高频短语）
function oralHint(text) {
  const low = String(text || '').toLowerCase();
  const hits = [];
  for (const [k, v] of Object.entries(ORAL)) {
    if (k.length >= 2 && low.includes(k)) { hits.push(v.term + '→' + v.val); if (hits.length >= 20) break; }
  }
  if (!hits.length) return '';
  return ' 口语处理（严格遵守）：' + hits.join('；') + '。标注「（删除）」的是口语填充词，直接省略不译；其余按给出的中文表达翻译。';
}
function recordOral(text) {
  if (!text) return;
  const t = String(text);
  const low = t.toLowerCase();
  for (const [k, v] of Object.entries(ORAL)) if (k.length >= 2 && low.includes(k)) ORAL_COUNT[k] = (ORAL_COUNT[k] || 0) + 1;
  // 候选：高频英文二元词 & 中文二字组合（用于自动发现新填充词）
  const words = low.replace(/[^a-z'\s]/g, ' ').split(/\s+/).filter(Boolean);
  for (let i = 0; i + 1 < words.length; i++) { const g = words[i] + ' ' + words[i + 1]; ORAL_CAND[g] = (ORAL_CAND[g] || 0) + 1; }
  const zh = t.replace(/[^\u4e00-\u9fa5]/g, '');
  for (let i = 0; i + 2 <= zh.length; i++) { const g = zh.slice(i, i + 2); if (g.length === 2) ORAL_CAND[g] = (ORAL_CAND[g] || 0) + 1; }
  capMap(ORAL_CAND, 4000); capMap(ORAL_COUNT, 2000);
}
async function mineOral() {
  if (MINE_BUSY || !AUTO_GLOSSARY || !LAST_CFG) return;
  const cands = Object.entries(ORAL_CAND)
    .filter(([k, v]) => v >= 8 && !ORAL[k.toLowerCase()] && !ORAL_MINED[k] && k.length >= 2)
    .sort((a, b) => b[1] - a[1]).slice(0, 25).map(([k]) => k);
  if (!cands.length) return;
  MINE_BUSY = true;
  try {
    const msgs = [
      { role: 'system', content: '你在为一个同声传译系统整理“口语填充词表”。用户会给出若干高频短语（可能来自英文或中文口语）。请判断哪些是**没有实义的口语填充词/口头禅**，哪些是**有实义、应翻译**的。只输出 JSON 对象：键为原短语；值为 "（删除）"（表示填充词，可省略）或该短语的简短中文表达。不属于口语词的条目请不要输出。' },
      { role: 'user', content: JSON.stringify(cands) },
    ];
    const out = await callChatOnce({ baseUrl: LAST_CFG.baseUrl, apiKey: LAST_CFG.apiKey, model: LAST_CFG.chatModel, messages: msgs });
    const json = String(out).replace(/^[^{]*/, '').replace(/[^}]*$/, '');
    const map = JSON.parse(json);
    let added = 0;
    for (const [k, v] of Object.entries(map)) {
      if (typeof v !== 'string' || !v.trim()) continue;
      ORAL_MINED[k] = { val: v.trim(), n: ORAL_CAND[k] || 1, auto: true };
      ORAL[k.toLowerCase()] = { term: k, val: v.trim(), auto: true };
      added += 1;
    }
    if (added) {
      capMap(ORAL_MINED, 400);
      try { writeFileSync(ORAL_MINED_FILE, JSON.stringify(ORAL_MINED, null, 2)); } catch {}
      console.log('[oral] 自动收录口语条目', added, '条，累计', Object.keys(ORAL_MINED).length);
    }
  } catch (e) { console.warn('[oral] 挖掘失败：', e.message); }
  finally { MINE_BUSY = false; }
}
function glossaryHint(text, extra) {
  const hits = [];
  const low = String(text || '').toLowerCase();
  for (const [k, v] of Object.entries(GLOSSARY)) {
    if (k.length >= 3 && low.includes(k)) { hits.push(v); if (hits.length >= 24) break; }
  }
  if (Array.isArray(extra)) {
    for (const e of extra.slice(0, 40)) {
      const parts = String(e).split('=');
      const a = (parts[0] || '').trim(), b = (parts[1] || '').trim();
      if (a && b && low.includes(a.toLowerCase())) hits.push(a + '→' + b);
    }
  }
  if (!hits.length) return '';
  return ' 术语对照（必须严格遵守这些译法）：' + hits.join('；') + '。';
}
// 明显是示例占位符的密钥不算“已配置”
const looksRealKey = (k) => !!k && k.length >= 16 && !/[你您<>\u201c\u201d填入]/.test(k);
const profilePublic = (code, p) => ({
  code,
  name: p.name,
  note: p.note,
  host: p.baseUrl.replace(/^https?:\/\//, '').replace(/\/+$/, ''),
  chatModel: p.chatModel,
  sttModel: p.sttModel,
  ttsModel: p.ttsModel,
  ttsVoice: p.ttsVoice,
  hasKey: looksRealKey(p.apiKey),
  refineModel: p.refineModel || '',
  hasRefine: looksRealKey(p.refineApiKey) && !!p.refineModel,   // 只暴露“有没有配”，绝不暴露 Key
});
const publicProfiles = () => Object.entries(PROFILESF()).map(([c, p]) => profilePublic(c, p));

// ---------- 小工具 ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function settingsFrom(headers, body) {
  // 解析顺序：客户端手动填写 > 预设代号(profiles.json) > 服务端 .env
  const h = (n) => (headers[n.toLowerCase()] || '').trim();
  const s = (body && typeof body.settings === 'object') ? body.settings : {};
  const prof = PROFILESF()[s.profile || h('x-profile') || ''] || null;
  const p = (k) => (prof && prof[k]) || '';
  return {
    profile: s.profile || h('x-profile') || '',
    baseUrl: (pick(s.baseUrl, h('x-ai-base-url'), p('baseUrl'), ENVF().baseUrl) || '').trim().replace(/\/+$/, ''),
    apiKey: pick(s.apiKey, h('x-ai-key'), p('apiKey'), ENVF().apiKey) || '',
    chatModel: pick(s.chatModel, h('x-ai-model'), p('chatModel'), ENVF().chatModel) || '',
    ttsModel: pick(s.ttsModel, p('ttsModel'), ENVF().ttsModel) || '',
    ttsVoice: pick(s.ttsVoice, h('x-ai-voice'), p('ttsVoice'), ENVF().ttsVoice) || '',
    sttModel: pick(s.sttModel, h('x-ai-stt-model'), p('sttModel'), ENVF().sttModel) || '',
    // 识别可独立走另一家（例如翻译用硅基、识别用 Groq）
    sttBaseUrl: (pick(s.sttBaseUrl, h('x-ai-stt-base-url'), p('sttBaseUrl'), p('baseUrl'), ENVF().baseUrl) || '').trim().replace(/\/+$/, ''),
    sttApiKey: pick(s.sttApiKey, h('x-ai-stt-key'), p('sttApiKey'), p('apiKey'), ENVF().apiKey) || '',
    sttApi: pick(s.sttApi, h('x-ai-stt-api'), p('sttApi'), ENVF().sttApi) || 'openai',
    fallbackSttApi: p('fallbackSttApi') || 'openai',
    fallbackSttBaseUrl: (p('fallbackSttBaseUrl') || '').trim().replace(/\/+$/, ''),
    fallbackSttApiKey: p('fallbackSttApiKey') || '',
    fallbackSttModel: p('fallbackSttModel') || '',
    refineBaseUrl: (pick(s.refineBaseUrl, p('refineBaseUrl'), ENVF().refineBaseUrl) || '').trim().replace(/\/+$/, ''),
    refineApiKey: pick(s.refineApiKey, p('refineApiKey'), ENVF().refineApiKey) || '',
    refineModel: pick(s.refineModel, p('refineModel'), ENVF().refineModel) || '',
    refineApi: pick(s.refineApi, p('refineApi'), ENVF().refineApi) || 'responses',
  };
}
// OpenAI Responses 协议（很多 GPT 中转站只支持这个，不支持 /chat/completions）
async function callResponsesOnce({ baseUrl, apiKey, model, instructions, input, timeoutMs = 60000 }) {
  const url = String(baseUrl || '').replace(/\/+$/, '') + '/responses';
  const bodyObj = { model, input, store: false, stream: false };
  if (instructions) bodyObj.instructions = instructions;
  const up = await fetchWithTimeout(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + apiKey,
      'User-Agent': 'codex_cli_rs/0.20.0',   // 部分中转站会校验 Codex 客户端标识
      originator: 'codex_cli_rs',
    },
    body: JSON.stringify(bodyObj),
  }, timeoutMs);
  const txt = await up.text();
  if (!up.ok) {
    const msg = '精修上游错误（HTTP ' + up.status + '）：' + txt.slice(0, 200);
    console.warn('[refine]', msg);
    throw Object.assign(new Error(msg), { status: 502 });
  }
  let j = {};
  try { j = JSON.parse(txt); } catch { throw new Error('精修返回无法解析'); }
  let out = typeof j.output_text === 'string' ? j.output_text : '';
  if (!out && Array.isArray(j.output)) {
    for (const item of j.output) {
      if (item && Array.isArray(item.content)) {
        for (const c of item.content) if (c && typeof c.text === 'string') out += c.text;
      }
    }
  }
  if (!out && j.choices?.[0]?.message?.content) out = j.choices[0].message.content;
  return String(out || '').trim();
}
// 音频前处理：16k 单声道 PCM16 WAV 的音量归一化（远场/小声说话时显著提升识别率）
function normalizeWav(buf) {
  try {
    if (buf.length < 44 || buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE') return buf;
    // 找 data 块
    let off = 12, dataOff = -1, dataLen = 0;
    while (off + 8 <= buf.length) {
      const id = buf.toString('ascii', off, off + 4);
      const sz = buf.readUInt32LE(off + 4);
      if (id === 'data') { dataOff = off + 8; dataLen = Math.min(sz, buf.length - off - 8); break; }
      off += 8 + sz + (sz % 2);
    }
    if (dataOff < 0 || dataLen < 320) return buf;
    const n = Math.floor(dataLen / 2);
    let sum = 0, peak = 0;
    const smp = new Int16Array(n);
    for (let i = 0; i < n; i++) { const v = buf.readInt16LE(dataOff + i * 2); smp[i] = v; sum += v * v; const a = Math.abs(v); if (a > peak) peak = a; }
    const rms = Math.sqrt(sum / n);
    if (rms < 1) return buf;                        // 近乎静音：不动
    if (rms >= 1200 || peak === 0) return buf;      // 音量正常 → 不做任何处理（实测改动反而有风险）
    let gain = 3600 / rms;
    if (gain > 4) gain = 4;                         // 最多放大 12 dB，只救“确实很小声”的录音
    if (peak * gain > 32767) gain = 32767 / peak;   // 防削波
    if (gain <= 1.05) return buf;
    for (let i = 0; i < n; i++) {
      let v = Math.round(smp[i] * gain);
      if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
      buf.writeInt16LE(v, dataOff + i * 2);
    }
    return buf;
  } catch { return buf; }
}

// ---------- 纠错表：用户纠正过的“错听 → 正确”（口音造成的固定错听，改一次永久生效）----------
const FIX_FILE = PE('FIX_FILE') || path.join(ROOT, 'corrections.json');
let FIXES = {};
try { if (existsSync(FIX_FILE)) FIXES = JSON.parse(readFileSync(FIX_FILE, 'utf8')) || {}; } catch {}
function applyFixes(text) {
  let t = String(text || '');
  for (const [w, r] of Object.entries(FIXES)) {
    if (!w || w.length < 2) continue;
    const esc = w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try { t = t.replace(new RegExp('\\b' + esc + '\\b', 'gi'), r); } catch {}
  }
  return t;
}

// 识别结果择优评分：内容越全、复读越少、语速越合理 → 分越高
function sttScore(t, secs) {
  const txt = String(t || '');
  const words = (txt.match(/[A-Za-z一-龥']+/g) || []);
  const n = words.length;
  if (!n) return -1;
  // 复读惩罚：重复的 3 词片段
  const seen = new Set(); let dup = 0;
  for (let i = 0; i + 3 <= words.length; i++) {
    const g = words.slice(i, i + 3).join(' ').toLowerCase();
    if (seen.has(g)) dup += 1; else seen.add(g);
  }
  const dens = n / Math.max(1, secs);          // 词/秒
  let score = n - dup * 2.5;
  if (dens < 1.0) score -= (1.0 - dens) * n * 0.8;   // 语速过低 → 疑似漏内容
  if (dens > 5.0) score -= (dens - 5.0) * n * 0.4;   // 语速过高 → 疑似幻觉灌水
  return score;
}

// OpenAI 兼容的 multipart 语音识别（硅基流动 / whisper 类接口通用）
async function sttViaOpenAI(buf, cfg, lang) {
  const fd = new FormData();
  fd.append('file', new Blob([buf], { type: 'audio/wav' }), 'clip.wav');
  fd.append('model', cfg.model || 'whisper-1');
  if (lang) fd.append('language', String(lang).slice(0, 5));
  fd.append('response_format', 'json');
  const hotText = Object.keys(GLOSSARY).map((k) => GLOSSARY[k].split('→')[0]).filter((t) => t.length >= 3).slice(0, 20).join(', ').slice(0, 220);
  if (hotText) fd.append('prompt', hotText);
  const up = await fetchWithTimeout(cfg.baseUrl.replace(/\/+$/, '') + '/audio/transcriptions', {
    method: 'POST', headers: { Authorization: 'Bearer ' + cfg.key }, body: fd,
  }, 40000);
  const txt = await up.text();
  if (!up.ok) throw new Error('识别端点 HTTP ' + up.status + '：' + txt.slice(0, 200));
  try { const j = JSON.parse(txt); return String(j.text || '').trim(); }
  catch { return txt.trim(); }
}

// 阿里云百炼（DashScope）语音识别：同步接口，支持 base64 音频（无需公网 URL）
async function callDashscopeAsr(buf, cfg, lang) {
  const url = (cfg.sttBaseUrl && cfg.sttBaseUrl.includes('dashscope'))
    ? cfg.sttBaseUrl.replace(/\/+$/, '') + '/services/aigc/multimodal-generation/generation'
    : 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation';
  const audio = 'data:audio/wav;base64,' + buf.toString('base64');
  const parameters = {};
  if (lang) parameters.asr_options = { language: String(lang).slice(0, 5) };
  const bodyObj = {
    model: cfg.sttModel || 'qwen3-asr-flash',
    input: { messages: [{ role: 'user', content: [{ audio }] }] },
  };
  if (Object.keys(parameters).length) bodyObj.parameters = parameters;
  const up = await fetchWithTimeout(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (cfg.sttApiKey || cfg.apiKey) },
    body: JSON.stringify(bodyObj),
  }, 40000);
  const txt = await up.text();
  if (!up.ok) throw new Error('百炼识别失败（HTTP ' + up.status + '）：' + txt.slice(0, 220));
  let j = {};
  try { j = JSON.parse(txt); } catch { throw new Error('百炼返回无法解析'); }
  let out = '';
  const ch = j.output && j.output.choices;
  if (Array.isArray(ch) && ch[0] && ch[0].message && Array.isArray(ch[0].message.content)) {
    for (const c of ch[0].message.content) if (c && typeof c.text === 'string') out += c.text;
  }
  if (!out && typeof j.output === 'string') out = j.output;
  return String(out || '').trim();
}

async function fetchWithTimeout(url, opts = {}, ms = 25000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try { return await fetch(url, { ...opts, signal: ctl.signal }); }
  finally { clearTimeout(timer); }
}
const okJson = (res, obj, code = 200) => {
  const buf = Buffer.from(JSON.stringify(obj));
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': buf.length, 'Cache-Control': 'no-store' });
  res.end(buf);
};
const errJson = (res, status, message) => okJson(res, { error: message }, status);
const sse = (res, event, data) => res.write('event: ' + event + '\ndata: ' + JSON.stringify(data) + '\n\n');
const pick = (...xs) => xs.find((x) => x !== undefined && x !== null && x !== '');

async function readBody(req, cap = MAX_AUDIO_BYTES) {
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > cap) throw Object.assign(new Error('请求体过大'), { status: 413 });
    chunks.push(c);
  }
  return Buffer.concat(chunks);
}
async function readJson(req, cap = 1 << 20) {
  try { return JSON.parse((await readBody(req, cap)).toString('utf8') || '{}'); }
  catch { return {}; }
}

// ---------- 同传翻译提示词（模拟成熟同传的"意义对意义"策略） ----------
function interpSystemPrompt(sourceName, targetName, style, hasCtx) {
  if (style === 'draft') {   // 实时草稿：严格模式，专治重复/中英混杂
    return '你是实时同声传译的草稿翻译器。输入是语音识别的实时片段：可能没有标点、可能没说完、可能含重复词。请把已有内容翻译成' + targetName + '。硬性要求：只输出' + targetName + '译文；除专有名词（人名/品牌/缩写）外不得出现' + sourceName + '单词；严禁重复任何词或短语；不要解释、不要加引号或前缀；输入不完整时只翻译已有部分，保持通顺简短。';
  }
  const base = style === 'interp'
    ? '你是顶级国际会议同声传译员，把用户的发言从' + sourceName + '即时口译成' + targetName +
      '。要求：意义对意义而非逐字翻译；译文自然流畅、符合' + targetName + '母语表达习惯；专有名词保留发音；口语中"嗯/啊/那个"等填充词直接省略；不添加任何解释、注释或前后缀；只输出译文本身。'
    : '你是一名专业口译助手，把用户的发言从' + sourceName + '翻译成' + targetName +
      '。要求：忠实且自然，符合' + targetName + '习惯；保留专有名词；省略口头禅；不解释、不加注、不输出多余内容，只输出译文本身。';
  if (hasCtx) {
    return base + ' 注意：历史对话是刚刚发生的上下文。请利用它保持人称、时态与指代连贯（如"它/他/这个/我们"等指代上文），只翻译最新一句原文，不要复述或重复历史内容。';
  }
  return base;
}

// ---------- OpenAI 兼容 /chat/completions（流式 + 30s 超时 + 网络抖动重试一次） ----------
const isNetErr = (m) => /fetch failed|failed to fetch|ECONN|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|abort|network|socket/i.test(m || '');
// 翻译任务不需要“思考链”：对混合推理模型显式关闭，能显著降低首字延迟
function withNoThink(bodyObj) {
  const m = String(bodyObj.model || '');
  if (/deepseek|qwen3|glm|kimi|minimax/i.test(m)) bodyObj.enable_thinking = false;
  return bodyObj;
}
async function chatFetch(baseUrl, apiKey, bodyObj) {
  withNoThink(bodyObj);
  const url = baseUrl + '/chat/completions';
  const opts = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + apiKey },
    body: JSON.stringify(bodyObj),
  };
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      return await fetchWithTimeout(url, opts, 30000);
    } catch (e) {
      if (isNetErr(e.message) && attempt === 0) { await sleep(900); continue; }
      throw Object.assign(new Error('连接翻译服务失败：' + e.message), { status: 502 });
    }
  }
  throw new Error('连接翻译服务失败');
}

async function callChatStream({ baseUrl, apiKey, model, messages, temperature }) {
  const upstream = await chatFetch(baseUrl, apiKey, { model, messages, temperature: temperature ?? 0.3, stream: true });
  if (!upstream.ok) {
    const detail = (await upstream.text()).slice(0, 600);
    throw Object.assign(new Error('上游返回 HTTP ' + upstream.status + '：' + detail), { status: 502 });
  }
  return upstream;
}

async function callChatOnce({ baseUrl, apiKey, model, messages }) {
  const upstream = await chatFetch(baseUrl, apiKey, { model, messages, temperature: 0.3, stream: false });
  const text = await upstream.text();
  if (!upstream.ok) throw Object.assign(new Error('上游返回 HTTP ' + upstream.status + '：' + text.slice(0, 600)), { status: 502 });
  try { return JSON.parse(text).choices?.[0]?.message?.content || ''; }
  catch { throw Object.assign(new Error('上游响应无法解析：' + text.slice(0, 300)), { status: 502 }); }
}

// ---------- 路由处理 ----------
const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json',
  '.woff2': 'font/woff2', '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
};

async function serveStatic(req, res, pathname) {
  let rel;
  try { rel = decodeURIComponent(pathname.replace(/^\//, '')) || 'index.html'; }
  catch { rel = 'index.html'; }
  if (rel.includes('..') || rel.includes('\\')) return errJson(res, 403, 'forbidden');
  const file = path.join(PUBLIC, rel);
  let data;
  try { data = await readFile(file); }
  catch {
    // SPA 风格回退到 index.html（本项目基本用不到）
    try { data = await readFile(path.join(PUBLIC, 'index.html')); }
    catch { return errJson(res, 404, 'not found'); }
  }
  const ext = path.extname(file).toLowerCase();
  res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream', 'Content-Length': data.length, 'Cache-Control': 'no-cache' });
  res.end(req.method === 'HEAD' ? undefined : data);
}

async function handleTranslate(req, res, body, isFinal) {
  const text = String(body.text || '').trim();
  if (!text) return errJson(res, 400, '缺少 text');
  const cfg = settingsFrom(req.headers, body);
  const mockNow = MOCKF() && !cfg.apiKey;
  if (mockNow) {
    if (isFinal) {
      if (body.task === 'clean') return okJson(res, { text });   // 演示模式：清洗原样返回
      const mock = mockTranslate(text, body.targetName || 'English');
      return okJson(res, { text: mock });
    }
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store' });
    sse(res, 'meta', { mock: true }); // eslint-disable-line
    const words = String(body.mockText || ('【模拟同传 · ' + (body.targetName || '译文') + '】 ' + text)).split(/(?<=[，。！？；\s])/u);
    for (const w of words) { if (w) { sse(res, 'delta', { d: w }); await sleep(45); } }
    sse(res, 'done', { text: words.join('') });
    return res.end();
  }
  if (!cfg.baseUrl) return errJson(res, 400, '未配置 AI_BASE_URL');
  if (!cfg.apiKey) return errJson(res, 400, '未配置 AI_API_KEY（服务端 .env 或浏览器设置均可）');
  const lim = overLimit(req, 'tr');            // 限额检查
  if (lim) return errJson(res, 429, lim);
  bumpUsage(req, 'tr');                        // 用量计数
  const sourceName = body.sourceName || '自动检测语言';
  const targetName = body.targetName || 'English';
  const langHint = body.sourceLang ? '（说话语言：' + sourceName + '，按此语言理解原文）' : '';
  if (body.task === 'clean') {   // ① 清洗识别文本（去口头禅/修错字/补标点）
    const msgs = [
      { role: 'system', content: '你是专业语音转写校对员。用户会给一段语音识别(ASR)原文，其中可能有：口头禅/填充词（嗯、呃、那个、you know、um…）、同音错别字、缺失的标点。请做最小必要修正：删除口头禅与无意义重复；结合语境改正明显同音错字；补全断句标点；不得改变语义、不得增删信息。只输出整理后的文本本身。' },
      { role: 'user', content: text },
    ];
    return okJson(res, { text: await callChatOnce({ ...cfg, model: cfg.chatModel, messages: msgs }) });
  }
  if (body.task === 'refine') {   // 定稿精修：可用独立供应商（Responses 协议）
    const src = String(body.text || '').trim();
    const draft = String(body.draft || '').trim();
    if (!src) return errJson(res, 400, '缺少 text');
    const gloss2 = glossaryHint(src, body.glossary);
    const instructions = '你是顶级中英同声传译译员。下面给出语音识别的原文与一份实时草稿译文，请输出更准确、通顺、符合中文表达习惯的定稿译文。要求：只输出译文本身；不要解释、不要加引号；不得增删原意；专有名词与专业术语必须准确' + gloss2 + oralHint(src) + NO_ELLIPSIS;
    const input = '【原文】' + '\n' + src + (draft ? '\n\n【草稿译文】\n' + draft : '');
    const rBase = cfg.refineBaseUrl || cfg.baseUrl;
    const rKey = cfg.refineApiKey || cfg.apiKey;
    const rModel = cfg.refineModel || cfg.chatModel;
    if (!rBase || !rKey) return errJson(res, 400, '未配置精修模型（refineBaseUrl / refineApiKey）');
    try {
      let out;
      if ((cfg.refineApi || 'responses') === 'responses') {
        out = await callResponsesOnce({ baseUrl: rBase, apiKey: rKey, model: rModel, instructions, input });
      } else {
        out = await callChatOnce({ baseUrl: rBase, apiKey: rKey, model: rModel, messages: [{ role: 'system', content: instructions }, { role: 'user', content: input }] });
      }
      if (!out) return errJson(res, 502, '精修模型未返回内容');
      bumpChars(req, src.length, out.length);
      return okJson(res, { text: out, model: rModel });
    } catch (e) {
      console.warn('[refine] 失败:', e && e.message);
      return errJson(res, e.status || 502, e.message);
    }
  }
  const history = Array.isArray(body.context)
    ? body.context
        .filter((m) => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
        .slice(-10)
        .map((m) => ({ role: m.role, content: m.content.slice(0, 500) }))
    : [];
  LAST_CFG = { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey, chatModel: cfg.chatModel };
  recordSource(text);   // 自动挖词：记录原文里的术语候选
  const gloss = glossaryHint(text, body.glossary);   // 术语表注入
  const messages = [
    { role: 'system', content: interpSystemPrompt(sourceName, targetName, body.style || 'interp', history.length > 0) + domainHint(body.domain, body.notes) + gloss + oralHint(text) + NO_ELLIPSIS },
    ...history,
    { role: 'user', content: '原文' + langHint + '：\n' + text },
  ];
  try {
    if (isFinal) {
      const out = await callChatOnce({ ...cfg, model: cfg.chatModel, messages });
      bumpChars(req, text.length, String(out || '').length);
      return okJson(res, { text: out });
    }
    const upstream = await callChatStream({ ...cfg, model: cfg.chatModel, messages });
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' });
    sse(res, 'meta', { model: cfg.chatModel });
    const ac = new AbortController();
    req.on('close', () => { try { ac.abort(); } catch {} });
    let buf = '', full = '', raw = '';
    const decode = new TextDecoder();
    const reader = upstream.body.getReader();   // 手动读：可对“卡住的流”做超时中断
    let stalled = false;
    for (;;) {
      let r;
      const stallTimer = setTimeout(() => {   // 上游 25 秒没数据 → 判定卡死，主动断开
        stalled = true;
        try { reader.cancel().catch(() => {}); } catch {}
      }, 25000);
      try { r = await reader.read(); }
      catch (e) { clearTimeout(stallTimer); throw e; }
      clearTimeout(stallTimer);
      if (r.done) break;
      if (stalled) break;
      const s = decode.decode(r.value, { stream: true });   // fetch 流是 Uint8Array
      raw += s;
      buf += s;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') continue;
        let j;
        try { j = JSON.parse(payload); } catch { continue; }
        const piece = j.choices?.[0]?.delta?.content;
        if (piece) { full += piece; sse(res, 'delta', { d: piece }); }
      }
    }
    if (stalled) console.warn('[stream] 上游停滞 25s 已中断 model=' + cfg.chatModel + ' 已产出=' + full.length + ' 字');
    // 兼容：某些上游忽略 stream 参数、返回普通 JSON
    if (!full) {
      try {
        const j = JSON.parse(raw);
        const c = j.choices?.[0]?.message?.content || j.choices?.[0]?.text || '';
        if (c) full = String(c);
      } catch {}
    }
    if (!full) {   // 上游没吐内容：明确告诉前端（否则界面会“静悄悄地不翻译”）
      console.warn('[stream] 上游无内容 model=' + cfg.chatModel + ' raw=' + raw.slice(0, 160));
      sse(res, 'error', { message: '上游无响应（timeout），请重试' });
      return res.end();
    }
    bumpChars(req, text.length, full.length);
    sse(res, 'done', { text: full });
    res.end();
  } catch (e) {
    console.warn('[translate] 失败:', e && e.message);   // 记录到 journalctl，便于排查
    if (res.headersSent) { try { sse(res, 'error', { message: e.message }); res.end(); } catch {} return; }
    errJson(res, e.status || 502, e.message);
  }
}

async function handleSpeech(req, res, body) {
  const text = String(body.text || '').trim();
  if (!text) return errJson(res, 400, '缺少 text');
  const cfg = settingsFrom(req.headers, body);
  const mockNow = MOCKF() && !cfg.apiKey;
  if (mockNow) { // 生成一段"哔-哔"提示音 WAV，用于离线演示语音链路
    const wav = beepWav(Math.min(Math.max(text.length, 3), 60));
    res.writeHead(200, { 'Content-Type': 'audio/wav', 'Content-Length': wav.length });
    return res.end(wav);
  }
  if (!cfg.baseUrl || !cfg.apiKey) return errJson(res, 400, 'NO_CLOUD_TTS：未配置云端 TTS（前端将自动回退浏览器语音合成）');
  bumpUsage(req, 'tts');
  try {
    const upstream = await fetch(cfg.baseUrl + '/audio/speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + cfg.apiKey },
      body: JSON.stringify({ model: cfg.ttsModel, voice: cfg.ttsVoice || 'nova', input: text, response_format: 'mp3' }),
    });
    const data = Buffer.from(await upstream.arrayBuffer());
    if (!upstream.ok) {
      const msg = data.toString('utf8').slice(0, 400);
      return errJson(res, 502, '云端 TTS 失败（HTTP ' + upstream.status + '）：' + msg + '（可切换为浏览器合成）');
    }
    res.writeHead(200, { 'Content-Type': 'audio/mpeg', 'Content-Length': data.length, 'Cache-Control': 'public, max-age=3600' });
    res.end(data);
  } catch (e) {
    errJson(res, 502, '云端 TTS 请求失败：' + e.message);
  }
}

async function handleSttProbe(req, res, body) {
  const cfg = settingsFrom(req.headers, body);
  const mockNow = MOCKF() && !cfg.apiKey;
  if (mockNow) return okJson(res, { ok: true, note: '演示模式：识别为模拟数据，未访问上游' });
  if (!cfg.baseUrl) return errJson(res, 400, '未配置 AI_BASE_URL（或选用提供语音识别的服务商）');
  if (!cfg.apiKey) return errJson(res, 400, '未配置 AI_API_KEY');
  try {
    if ((cfg.sttApi || 'openai') === 'dashscope') {   // 探测百炼通道
      const t = await callDashscopeAsr(Buffer.from(beepWav(2)), { sttBaseUrl: cfg.sttBaseUrl, sttApiKey: cfg.sttApiKey || cfg.apiKey, sttModel: cfg.sttModel }, 'en');
      return okJson(res, { ok: true, via: 'dashscope', note: '百炼识别通道正常（返回文本长度 ' + String(t).length + '）' });
    }
    const fd = new FormData();
    fd.append('file', new Blob([beepWav(2)], { type: 'audio/wav' }), 'probe.wav');
    fd.append('model', cfg.sttModel || 'whisper-1');
    fd.append('language', 'en');
    fd.append('response_format', 'json');
    let up;
    try {
      up = await fetchWithTimeout((cfg.sttBaseUrl || cfg.baseUrl) + '/audio/transcriptions', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + (cfg.sttApiKey || cfg.apiKey) },
        body: fd,
      }, 25000);
    } catch (e) { return errJson(res, 504, '识别通道超时（25s）：模型可能在排队，请换识别模型'); }
    const txt = (await up.text()).slice(0, 300);
    if (up.ok) return okJson(res, { ok: true, via: 'openai', note: '通道正常（HTTP ' + up.status + '）' });
    return errJson(res, 502, '识别端点 HTTP ' + up.status + '：' + txt + ' —— 请确认服务商提供 /audio/transcriptions 且模型名正确');
  } catch (e) {
    errJson(res, 502, '无法连接识别端点：' + e.message);
  }
}

async function handleStt(req, res, query) {
  // 客户端把整段录音以原始二进制 POST 过来；这里按主通道识别，失败自动切备用通道
  const cfg = settingsFrom(req.headers, {});
  const lang = (query.get('language') || '').trim();
  const mockNow = MOCKF() && !cfg.apiKey;
  if (mockNow) {
    const canned = mockSttText(lang);
    return okJson(res, { text: canned.text, language: canned.language, mock: true });
  }
  if (!cfg.baseUrl || !cfg.apiKey) return errJson(res, 400, '未配置云端语音识别（或改用浏览器识别引擎）');
  let buf;
  try { buf = await readBody(req); } catch (e) { return errJson(res, e.status || 400, e.message); }
  if (buf.length < 100) return errJson(res, 400, '音频为空');
  const secs = estAudioSeconds(buf.length, req.headers['content-type']);
  const limStt = overLimit(req, 'stt', secs);
  if (limStt) return errJson(res, 429, limStt);
  bumpUsage(req, 'stt', secs);

  const isFinalSeg = String(req.headers['x-lb-final'] || '') === '1';   // 权威分段（段落定稿用）才做双通道
  buf = normalizeWav(buf);   // 音频前处理：音量归一化
  const primary = { api: cfg.sttApi || 'openai', baseUrl: cfg.sttBaseUrl || cfg.baseUrl, key: cfg.sttApiKey || cfg.apiKey, model: cfg.sttModel };
  const alt = (cfg.fallbackSttBaseUrl && cfg.fallbackSttApiKey)
    ? { api: cfg.fallbackSttApi || 'openai', baseUrl: cfg.fallbackSttBaseUrl, key: cfg.fallbackSttApiKey, model: cfg.fallbackSttModel }
    : null;
  const runOne = async (ch) => {
    const raw = (ch.api === 'dashscope')
      ? await callDashscopeAsr(buf, { sttBaseUrl: ch.baseUrl, sttApiKey: ch.key, sttModel: ch.model }, lang)
      : await sttViaOpenAI(buf, ch, lang);
    return applyFixes(raw);   // 用户纠错表：改一次，之后永久生效
  };

  // 结果可用性判断：词数过少（相对音频时长）→ 疑似漏内容，需要交叉复核
  const wordCount = (t) => (String(t || '').match(/[A-Za-z一-龥']+/g) || []).length;
  const looksWeak = (t, secs) => {
    const n = wordCount(t);
    if (n === 0) return true;
    return secs >= 6 && n / Math.max(1, secs) < 1.1;   // 正常口语约 2~3 词/秒
  };
  try {
    const text = await runOne(primary);
    if (alt && looksWeak(text, secs)) {   // 主通道疑似漏内容 → 用备用通道交叉复核
      try {
        const textAlt = await runOne(alt);
        console.warn('[stt] 主通道疑似漏词(' + wordCount(text) + '词/' + Math.round(secs) + 's)，交叉复核：备用 ' + wordCount(textAlt) + ' 词');
        if (wordCount(textAlt) > wordCount(text) * 1.3) {
          bumpChars(req, 0, textAlt.length);
          return okJson(res, { text: textAlt, language: lang || '', via: 'crosscheck' });
        }
      } catch (e3) { console.warn('[stt] 交叉复核失败:', e3 && e3.message); }
    }
    bumpChars(req, 0, text.length);
    return okJson(res, { text, language: lang || '', via: primary.api });
  } catch (e) {
    console.warn('[stt] 主识别失败(' + primary.api + '):', e && e.message);
    if (!alt) return errJson(res, 502, e.message);
    try {
      const text2 = await runOne(alt);
      bumpChars(req, 0, text2.length);
      console.warn('[stt] 已自动切换备用识别通道');
      return okJson(res, { text: text2, language: lang || '', via: 'fallback' });
    } catch (e2) {
      return errJson(res, 502, '主/备识别均失败：' + e.message + ' | ' + e2.message);
    }
  }
}

// ---------- 演示模式的模拟数据 ----------
// 演示模式：让连续成段返回的“模拟识别句”在不同句间轮换，
// 并让识别语言与请求一致——避免“每段都返回同一句”造成误解
const MOCK_POOL = [
  { text: '今天会议的核心议题，是人工智能如何改变制造业的生产流程。', language: 'chinese' },
  { text: '我们预计下一季度产能可以提升百分之二十左右。', language: 'chinese' },
  { text: 'Thank you for joining us. Let us start with the quarterly report.', language: 'english' },
  { text: 'The new model shows a significant improvement in latency and accuracy.', language: 'english' },
  { text: 'Could you please send me the updated agenda after the meeting?', language: 'english' },
  { text: 'We should focus on what the customer really needs first.', language: 'english' },
];
const LANG_MAP = { zh: 'chinese', en: 'english' };
const mockCounters = {};
function mockSttText(lang) {
  const key = LANG_MAP[lang] || lang || 'chinese';
  const pool = MOCK_POOL.filter((x) => x.language === key);
  const list = pool.length ? pool : MOCK_POOL;
  mockCounters[key] = (mockCounters[key] || 0) + 1;
  return list[(mockCounters[key] - 1) % list.length];
}
function mockTranslate(text, targetName) {
  const pairs = [
    ['今天会议的核心议题，是人工智能如何改变制造业的生产流程。',
     'The core topic of today\'s meeting is how AI is changing manufacturing production processes.'],
    ['我们预计下一季度产能可以提升百分之二十左右。',
     'We expect production capacity to grow about 20 percent next quarter.'],
    ['Thank you for joining us. Let us start with the quarterly report.',
     '感谢大家参会。我们先从季度报告开始。'],
    ['The new model shows a significant improvement in latency and accuracy.',
     '新模型在延迟和准确率方面都有显著改进。'],
    ['Could you please send me the updated agenda after the meeting?',
     '会议结束后，能把更新后的议程发给我吗？'],
    ['We should focus on what the customer really needs first.',
     '我们应该先关注客户真正需要什么。'],
  ];
  for (const [a, b] of pairs) {
    if (text.trim() === a.trim()) {
      const out = targetName.indexOf('中') >= 0 ? b : a;
      return '[模拟同传→' + targetName + '] ' + out;
    }
  }
  const zh2en = { '你好': 'Hello', '谢谢': 'Thank you', '我们': 'we', '今天': 'today', '会议': 'the meeting', '人工智能': 'artificial intelligence' };
  const en2zh = { 'thank you': '谢谢你', 'hello': '你好', 'meeting': '会议', 'today': '今天', 'quarter': '季度' };
  let out = text;
  const dict = targetName.indexOf('中') >= 0 ? en2zh : zh2en;
  for (const [k, v] of Object.entries(dict)) {
    if (targetName.indexOf('中') >= 0) out = out.replace(new RegExp('\\b' + k + '\\b', 'gi'), v);
    else out = out.split(k).join(v);
  }
  return '[模拟同传→' + targetName + '] ' + out.slice(0, 220);
}
function extOf(contentType) {
  if (contentType.includes('wav') || contentType.includes('wave')) return '.wav';
  if (contentType.includes('ogg')) return '.ogg';
  if (contentType.includes('mp4')) return '.mp4';
  if (contentType.includes('mpeg') || contentType.includes('mp3')) return '.mp3';
  return '.webm';
}
function beepWav(n) {
  const rate = 16000, secs = 0.12 * n + 0.15, len = Math.floor(rate * secs);
  const data = Buffer.alloc(44 + len * 2);
  data.write('RIFF', 0); data.writeUInt32LE(36 + len * 2, 4); data.write('WAVE', 8);
  data.write('fmt ', 12); data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
  data.writeUInt32LE(rate, 24); data.writeUInt32LE(rate * 2, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
  data.write('data', 36); data.writeUInt32LE(len * 2, 40);
  for (let i = 0; i < len; i++) {
    const t = i / rate;
    const seg = Math.floor(t / 0.12) % Math.max(n, 1);
    const env = Math.max(0, Math.sin(Math.PI * Math.min(t % 0.12, 0.12) / 0.12));
    const f = seg % 2 === 0 ? 880 : 660;
    const v = Math.round(Math.sin(2 * Math.PI * f * t) * env * 0.35 * 32767);
    data.writeInt16LE(v, 44 + i * 2);
  }
  return data;
}

// ---------- HTTP 服务 ----------
async function handleNodeRequest(req, res) {
  const u = new URL(req.url, 'http://' + req.headers.host || 'http://localhost');
  try {
    if (u.pathname === '/api/health' && req.method === 'GET') {
      return okJson(res, { ok: true, mock: MOCKF(), envConfigured: !!ENVF().apiKey, baseUrl: ENVF().baseUrl || null });
    }
    if (u.pathname === '/api/config' && req.method === 'GET') {
      return okJson(res, {
        mock: MOCKF(),
        envConfigured: !!ENVF().apiKey,
        baseUrl: ENVF().baseUrl || null,
        chatModel: ENVF().chatModel, sttModel: ENVF().sttModel,
        ttsModel: ENVF().ttsModel, ttsVoice: ENVF().ttsVoice,
        profiles: publicProfiles(),
        glossaryMined: Object.keys(MINED).length,
        oralMined: Object.keys(ORAL_MINED).length,
        oralFixed: ORAL_FIXED_N,
      });
    }
    if (u.pathname === '/api/profiles' && req.method === 'GET') {
      return okJson(res, { profiles: publicProfiles() });
    }
    if (u.pathname === '/api/usage' && req.method === 'GET') {
      const tok = PE('ADMIN_TOKEN') || '';
      if (tok && u.searchParams.get('token') !== tok) return errJson(res, 403, 'forbidden');
      resetUsageIfNewDay();
      const top = Object.entries(USAGE.perIp).sort((a, b) => (b[1].sttSec + b[1].trCalls / 100) - (a[1].sttSec + a[1].trCalls / 100)).slice(0, 20)
        .map(([ip, v]) => ({ ip, sttMinutes: +(v.sttSec / 60).toFixed(1), translateCalls: v.trCalls, ttsCalls: v.ttsCalls || 0, cost: estimateCost(v).total }));
      return okJson(res, {
        day: USAGE.day,
        limits: { enabled: LIMITS.enabled, perIpSttHours: LIMITS.perIpSttSec / 3600, perIpTranslateCalls: LIMITS.perIpTrCalls, globalSttHours: LIMITS.globalSttSec / 3600, globalTranslateCalls: LIMITS.globalTrCalls },
        total: {
          sttMinutes: +(USAGE.total.sttSec / 60).toFixed(1),
          sttHours: +((USAGE.total.sttSec || 0) / 3600).toFixed(2),
          sttCalls: USAGE.total.sttCalls,
          translateCalls: USAGE.total.trCalls,
          charsIn: USAGE.total.charsIn || 0,
          charsOut: USAGE.total.charsOut || 0,
          tokensIn: Math.round((USAGE.total.charsIn || 0) / 4),
          tokensOut: Math.round((USAGE.total.charsOut || 0) / 1.6),
        },
        cost: { ...estimateCost(USAGE.total), currency: 'CNY', prices: PRICE, note: '按字数/时长估算，可在服务端环境变量调整单价' },
        topIps: top,
      });
    }
    if (u.pathname === '/api/usage/reset' && req.method === 'POST') {
      const tok = PE('ADMIN_TOKEN') || '';
      if (tok && u.searchParams.get('token') !== tok) return errJson(res, 403, 'forbidden');
      USAGE = { day: todayStr(), total: { sttSec: 0, sttCalls: 0, trCalls: 0, ttsCalls: 0 }, perIp: {} };
      try { writeFileSync(USAGE_FILE, JSON.stringify(USAGE)); } catch {}
      return okJson(res, { ok: true, day: USAGE.day });
    }
    // ================= 管理员面板 =================
    if (u.pathname.startsWith('/api/admin/')) {
      if (!adminAuth(req, u)) return errJson(res, 403, 'forbidden');
      const os = nodeRequire('node:os');
      if (u.pathname === '/api/admin/overview') {
        resetUsageIfNewDay();
        const mem = { totalMB: Math.round(os.totalmem() / 1048576), freeMB: Math.round(os.freemem() / 1048576) };
        const load = os.loadavg().map((x) => +x.toFixed(2));
        let disk = null;
        try {
          const cp = nodeRequire('node:child_process');
          const out = cp.execSync("df -h / | tail -1", { encoding: 'utf8' }).trim().split(/\s+/);
          disk = { size: out[1], used: out[2], avail: out[3], usePct: out[4] };
        } catch {}
        return okJson(res, {
          now: Date.now(),
          uptimeSec: Math.round(process.uptime()),
          osUptimeSec: Math.round(os.uptime()),
          mem, load, disk,
          node: process.version,
          usage: { day: USAGE.day, total: USAGE.total, cost: estimateCost(USAGE.total), ips: Object.keys(USAGE.perIp).length },
          limits: { enabled: LIMITS.enabled, perIpSttHours: LIMITS.perIpSttSec / 3600, perIpTranslateCalls: LIMITS.perIpTrCalls, globalSttHours: LIMITS.globalSttSec / 3600, globalTranslateCalls: LIMITS.globalTrCalls },
          providers: Object.entries(PROFILESF()).map(([code, p2]) => ({
            code, name: p2.name || code, hasKey: looksRealKey(p2.apiKey),
            sttApi: p2.sttApi || 'openai', sttModel: p2.sttModel || '',
            sttFallback: p2.fallbackSttModel || '', chatModel: p2.chatModel || '',
            refineModel: p2.refineModel || '', refineApi: p2.refineApi || '',
          })),
          libs: {
            glossaryFixed: Object.keys(GLOSSARY).length - Object.keys(MINED).length,
            glossaryAuto: Object.keys(MINED).length,
            oralFixed: ORAL_FIXED_N, oralAuto: Object.keys(ORAL_MINED).length,
            fixes: Object.keys(FIXES).length,
          },
          counters: { sttCalls: USAGE.total.sttCalls, trCalls: USAGE.total.trCalls, logs: LOG_RING.length },
        });
      }
      if (u.pathname === '/api/admin/logs') {
        return okJson(res, { logs: LOG_RING.slice(-200).reverse() });
      }
      if (u.pathname === '/api/admin/libraries') {
        return okJson(res, {
          glossaryAuto: MINED, oralAuto: ORAL_MINED, fixes: FIXES,
          glossaryFixedCount: Object.keys(GLOSSARY).length - Object.keys(MINED).length,
          oralFixedCount: ORAL_FIXED_N,
        });
      }
      if (u.pathname === '/api/admin/library/delete' && req.method === 'POST') {
        const b = await readJson(req, 64 * 1024);
        const kind = String(b.kind || ''), key = String(b.key || '').trim();
        if (!key) return errJson(res, 400, '缺少 key');
        if (kind === 'term') { delete MINED[key]; delete GLOSSARY[key.toLowerCase()]; try { writeFileSync(MINED_FILE, JSON.stringify(MINED, null, 2)); } catch {} }
        else if (kind === 'oral') { delete ORAL_MINED[key]; delete ORAL[key.toLowerCase()]; try { writeFileSync(ORAL_MINED_FILE, JSON.stringify(ORAL_MINED, null, 2)); } catch {} }
        else if (kind === 'fix') { delete FIXES[key]; try { writeFileSync(FIX_FILE, JSON.stringify(FIXES, null, 2)); } catch {} }
        else return errJson(res, 400, 'kind 必须是 term / oral / fix');
        console.log('[admin] 删除条目', kind, key);
        return okJson(res, { ok: true });
      }
      if (u.pathname === '/api/admin/library/add' && req.method === 'POST') {
        const b = await readJson(req, 64 * 1024);
        const kind = String(b.kind || ''), key = String(b.key || '').trim(), val = String(b.value || '').trim();
        if (!key || !val) return errJson(res, 400, '缺少 key / value');
        if (kind === 'fix') { FIXES[key] = val; try { writeFileSync(FIX_FILE, JSON.stringify(FIXES, null, 2)); } catch {} }
        else if (kind === 'term') { MINED[key] = { zh: val, auto: false, manual: true }; GLOSSARY[key.toLowerCase()] = key + '→' + val; try { writeFileSync(MINED_FILE, JSON.stringify(MINED, null, 2)); } catch {} }
        else if (kind === 'oral') { ORAL_MINED[key] = { val, auto: false, manual: true }; ORAL[key.toLowerCase()] = { term: key, val }; try { writeFileSync(ORAL_MINED_FILE, JSON.stringify(ORAL_MINED, null, 2)); } catch {} }
        else return errJson(res, 400, 'kind 必须是 term / oral / fix');
        console.log('[admin] 新增条目', kind, key, '→', val);
        return okJson(res, { ok: true });
      }
      if (u.pathname === '/api/admin/probe' && req.method === 'POST') {   // 通道自检
        try {
          const buf = Buffer.from(beepWav(2));
          const p2 = PROFILESF().snow || Object.values(PROFILESF())[0] || {};
          const out = {};
          if ((p2.sttApi || 'openai') === 'dashscope') {
            try { await callDashscopeAsr(buf, { sttBaseUrl: p2.sttBaseUrl, sttApiKey: p2.sttApiKey || p2.apiKey, sttModel: p2.sttModel }, 'en'); out.sttMain = 'ok'; }
            catch (e) { out.sttMain = 'fail: ' + e.message; }
          } else { out.sttMain = 'skip(非 dashscope)'; }
          if (p2.fallbackSttModel) {
            try { await sttViaOpenAI(buf, { baseUrl: p2.fallbackSttBaseUrl, key: p2.fallbackSttApiKey, model: p2.fallbackSttModel }, 'en'); out.sttFallback = 'ok'; }
            catch (e) { out.sttFallback = 'fail: ' + e.message; }
          }
          return okJson(res, out);
        } catch (e) { return errJson(res, 500, e.message); }
      }
      return errJson(res, 404, 'unknown admin api');
    }
    if (u.pathname === '/api/correct' && req.method === 'POST') {   // 用户纠错：错听 → 正确
      const b = await readJson(req, 64 * 1024);
      const wrong = String(b.wrong || '').trim().slice(0, 120);
      const right = String(b.right || '').trim().slice(0, 120);
      if (!wrong || !right) return errJson(res, 400, '缺少 wrong / right');
      FIXES[wrong] = right;
      const keys = Object.keys(FIXES);
      if (keys.length > 800) delete FIXES[keys[0]];
      try { writeFileSync(FIX_FILE, JSON.stringify(FIXES, null, 2)); } catch {}
      GLOSSARY[wrong.toLowerCase()] = wrong + '→' + right;   // 翻译端也保持一致
      console.log('[fix] 新增纠错:', wrong, '→', right, '（共', Object.keys(FIXES).length, '条）');
      return okJson(res, { ok: true, count: Object.keys(FIXES).length });
    }
    if (u.pathname === '/api/fixes' && req.method === 'GET') {
      return okJson(res, { count: Object.keys(FIXES).length, fixes: FIXES });
    }
    if (u.pathname === '/api/glossary' && req.method === 'GET') {
      return okJson(res, { auto: !!(PE('AUTO_GLOSSARY') !== '0'), mined: MINED, minedCount: Object.keys(MINED).length, fixedCount: Object.keys(GLOSSARY).length - Object.keys(MINED).length, oral: ORAL_MINED, oralMinedCount: Object.keys(ORAL_MINED).length, oralFixedCount: ORAL_FIXED_N });
    }
    if (u.pathname === '/api/translate' && req.method === 'POST') {
      const body = await readJson(req);
      return await handleTranslate(req, res, body, u.searchParams.get('final') === '1');
    }
    if (u.pathname === '/api/speech' && req.method === 'POST') {
      const body = await readJson(req);
      return await handleSpeech(req, res, body);
    }
    if (u.pathname === '/api/stt' && req.method === 'POST') {
      return handleStt(req, res, u.searchParams);
    }
    if (u.pathname === '/api/sttprobe' && req.method === 'POST') {
      const body = await readJson(req);
      return await handleSttProbe(req, res, body);
    }
    if (u.pathname.startsWith('/api/')) return errJson(res, 404, 'unknown api');
    return serveStatic(req, res, u.pathname);
  } catch (e) {
    console.error('[server]', e);
    try { errJson(res, e.status || 500, e.message || 'internal error'); } catch { res.end(); }
  }
}

// ════════════════════════════════════════════════════════════════════════
//  Worker 入口：把 Request 适配成原来的 req/res，再交给上面那套路由
// ════════════════════════════════════════════════════════════════════════

/** 把 Request 包成原代码期望的 req（有 headers/method/url，且可 for await 读 body） */
function makeReq(request, url) {
  const headers = {};
  for (const [k, v] of request.headers) headers[k.toLowerCase()] = v;
  const chunks = [];
  const body = request.body;
  const req = {
    method: request.method,
    url: url.pathname + url.search,
    headers,
    on() {},
    socket: { remoteAddress: headers["cf-connecting-ip"] || "" },
    async *[Symbol.asyncIterator]() {
      if (!body) return;
      const reader = body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        const u8 = value instanceof Uint8Array ? value : new Uint8Array(value);
        chunks.push(u8);
        yield u8;
      }
    },
  };
  return req;
}

/** 把原代码写入的 res 收集成 Response；SSE 走流式，普通响应走缓冲 */
function makeRes(wantsStream) {
  const enc = new TextEncoder();
  let status = 200;
  let headers = {};
  let controller = null;
  let head = [];
  let closed = false;
  const stream = wantsStream
    ? new ReadableStream({
        start(c) {
          controller = c;
          if (head.length) { for (const h of head) c.enqueue(enc.encode(h)); head = []; }
        },
      })
    : null;
  const res = {
    writeHead(code, hdrs) {
      status = code;
      if (hdrs) Object.assign(headers, hdrs);
    },
    setHeader(k, v) { headers[k] = v; },
    write(s) {
      const text = typeof s === "string" ? s : new TextDecoder().decode(s);
      if (stream) { if (controller) controller.enqueue(enc.encode(text)); else head.push(text); }
      else head.push(text);
    },
    end(s) {
      if (closed) return;
      closed = true;
      if (s !== undefined && s !== null) res.write(s);
      if (stream) { if (controller) { try { controller.close(); } catch (e) {} } }
    },
    get _body() { return head.join(""); },
    get _status() { return status; },
    get _headers() { return headers; },
  };
  return { res, stream };
}

export default {
  async fetch(request, env, ctx) {
    LIVE_ENV = env || {};
    LIVE_CTX = ctx || null;
    LIVE_USER = null;

    let url;
    try { url = new URL(request.url); } catch (e) { return new Response("bad url", { status: 400 }); }

    // 登录门：静态页面与健康检查放行，其余 API 必须带账户会话
    if (url.pathname.startsWith("/api/")
        && url.pathname !== "/api/health" && url.pathname !== "/api/healthz" && url.pathname !== "/api/me") {
      let who = null;
      try { who = env.DB ? await currentUser(env, request) : null; } catch (e) { who = null; }
      LIVE_USER = who;
      if (!who) {
        return new Response(JSON.stringify({
          error: "请先登录后再使用同声传译：https://account.ourmetaverse.cn （注册只要一个邮箱收验证码）",
        }), { status: 401, headers: { "content-type": "application/json; charset=utf-8" } });
      }
    }

    if (url.pathname === "/api/healthz") {
      return new Response(JSON.stringify({ ok: true, service: "aurora-live" }), {
        headers: { "content-type": "application/json" },
      });
    }

    if (url.pathname === "/api/me") {
      let who = null;
      try { who = env.DB ? await currentUser(env, request) : null; } catch (e) { who = null; }
      return new Response(JSON.stringify(who
        ? { ok: true, logged_in: true, user: { email: who.email, role: who.role } }
        : { ok: true, logged_in: false }), { headers: { "content-type": "application/json; charset=utf-8" } });
    }

    // 同传计费：STT 上传的音频按秒累计，每满 60 秒扣 1 页额度
    const meterStt = url.pathname === "/api/stt" && request.method === "POST";
    let audioBytes = 0;
    if (meterStt) audioBytes = Number(request.headers.get("content-length") || 0);

    const wantsStream = url.pathname === "/api/translate" && request.method === "POST" && url.searchParams.get("final") !== "1";
    const { res, stream } = makeRes(wantsStream);
    const req = makeReq(request, url);

    try {
      await handleNodeRequest(req, res);
    } catch (err) {
      if (stream) { try { controllerClose(stream); } catch (e) {} }
      return new Response(JSON.stringify({ error: String((err && err.message) || err) }), {
        status: 500, headers: { "content-type": "application/json" },
      });
    }

    // 计费（只在接口成功时）：把本次音频秒数累加进 D1，满一分钟才扣 1 页
    if (meterStt && res._status >= 200 && res._status < 300 && audioBytes > 0 && LIVE_USER && env.DB) {
      try {
        const secs = estAudioSeconds(audioBytes, request.headers.get("content-type") || "");
        if (secs > 0) await meterLive(env, LIVE_USER.id, secs);
      } catch (e) { /* 计费失败不影响翻译 */ }
    }

    const outHeaders = new Headers();
    for (const [k, v] of Object.entries(res._headers || {})) {
      if (k.toLowerCase() === "content-length") continue;
      outHeaders.set(k, String(v));
    }
    if (stream) {
      if (!outHeaders.has("content-type")) outHeaders.set("content-type", "text/event-stream; charset=utf-8");
      outHeaders.set("cache-control", "no-store");
      return new Response(stream, { status: res._status, headers: outHeaders });
    }
    const body = res._body;
    if (!outHeaders.has("content-type")) outHeaders.set("content-type", "application/json; charset=utf-8");
    return new Response(body || "", { status: res._status, headers: outHeaders });
  },
};

function controllerClose(stream) {
  try { stream.cancel(); } catch (e) {}
}
