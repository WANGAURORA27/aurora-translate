#!/usr/bin/env python3
"""把 live-interpreter（Node 版）自动转成 Cloudflare Worker。

为什么这样做：server.mjs 有 1200 行、~900 行是纯业务逻辑（提示词、术语表、
SSE 流式解析、识别打分、纠错表、限额…）。**重写一遍风险极高**，所以这里只做
四件事，业务代码原样保留：

  1. 去掉 4 个 Node 专有 import，改用适配层提供的同名实现；
  2. 把模块级的 ENV / MOCK / PROFILES 变成"每次请求读一次"的函数（Worker 拿不到全局 env）；
  3. 把 `http.createServer(handler)` 改成普通函数，静态托管换成内置资源表；
  4. 末尾加上 Worker 的 fetch 入口（Request/Response ↔ 原本的 req/res 适配）。

用法：python3 cf/live/build.py      → 生成 cf/live/worker.js 与 cf/live/assets.js
可重复执行；改了原版 server.mjs 后重跑即可。
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC_DIR = os.path.join(os.path.dirname(REPO), "live-interpreter")
SRC = os.path.join(SRC_DIR, "server.mjs")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def gen_assets() -> str:
    """把前端静态文件打包成一个 JS 模块（字符串常量），避免依赖打包器规则。"""
    pub = os.path.join(SRC_DIR, "public")
    items = []
    for name in sorted(os.listdir(pub)):
        full = os.path.join(pub, name)
        if not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8") as fh:
            items.append((name, fh.read()))
    out = ["// 由 cf/live/build.py 自动生成：前端静态资源（原样搬运，仅 index.html 注入登录横幅）"]
    out.append("export const ASSETS = {")
    for name, text in items:
        if name == "index.html":
            text = text.replace("</body>", LOGIN_BANNER + "\n</body>") if "</body>" in text else text + LOGIN_BANNER
        if name == "app.js":
            # 一次性迁移：默认不朗读译文（想要播报可在设置里重新打开）
            text = text.replace(
                "if (!S._v12) { S.maxSegMs = 5000; S.tailMs = 600; S._v12 = 1; }",
                "if (!S._v12) { S.maxSegMs = 5000; S.tailMs = 600; S._v12 = 1; }\n"
                "if (!S._v13) { S.voiceOn = false; S._v13 = 1; saveS(); }   // v13：默认关闭语音播报", 1)
        out.append("  %s: %s," % (json.dumps(name), json.dumps(text, ensure_ascii=False)))
    out.append("};")
    # 术语表：live 自带 + 文档翻译那份（978 条经管术语）+ 数学课常用词
    merged = {}
    try:
        merged.update(json.loads(read(os.path.join(SRC_DIR, "glossary.json"))))
    except Exception:
        pass
    doc_glossary = os.path.join(REPO, "glossary.json")
    if os.path.isfile(doc_glossary):
        try:
            doc = json.loads(read(doc_glossary))
            for k, v in doc.items():
                if str(k).startswith("_") or not isinstance(v, dict):
                    continue
                merged[k] = v
        except Exception:
            pass
    merged["数学与微积分"] = {
        "power": "幂", "power rule": "幂法则", "power chain rule": "幂函数的链式法则",
        "chain rule": "链式法则", "derivative": "导数", "differentiate": "求导",
        "integral": "积分", "integration": "积分", "limit": "极限", "continuous": "连续",
        "function": "函数", "outer function": "外层函数", "inner function": "内层函数",
        "composite function": "复合函数", "exponent": "指数", "logarithm": "对数",
        "matrix": "矩阵", "vector": "向量", "theorem": "定理", "proof": "证明",
        "partial derivative": "偏导数", "gradient": "梯度", "maxima": "极大值",
        "minima": "极小值", "optimisation": "最优化", "constraint": "约束",
    }
    out.append("export const GLOSSARY_TEXT = %s;" % json.dumps(merged, ensure_ascii=False))
    out.append("export const ORAL_TEXT = %s;" % json.dumps(read(os.path.join(SRC_DIR, "oral.json")), ensure_ascii=False))
    out.append("")
    return "\n".join(out)


LOGIN_BANNER = """
<!-- 由构建脚本注入：未登录时提示去登录（不改动 app.js） -->
<div id="lb-gate" style="display:none;position:fixed;left:50%;top:14px;transform:translateX(-50%);z-index:9999;
     background:#7f1d1d;color:#fff;padding:10px 16px;border-radius:10px;font-size:14px;
     box-shadow:0 6px 24px rgba(0,0,0,.35);max-width:92vw;text-align:center;line-height:1.6">
  还没登录，同声传译用不了 &nbsp;·&nbsp;
  <a href="https://account.ourmetaverse.cn/" target="_blank" rel="noopener"
     style="color:#fde68a;text-decoration:underline">去登录 / 注册</a>
  <span style="opacity:.75">（注册只要一个邮箱收验证码）</span>
</div>
<script>
(function () {
  function check() {
    fetch('/api/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      var el = document.getElementById('lb-gate');
      if (el) el.style.display = (d && d.logged_in) ? 'none' : 'block';
    }).catch(function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', check);
  else check();
  setInterval(check, 60000);
})();
</script>
"""

PRELUDE = r'''
// ════════════════════════════════════════════════════════════════════════
//  以下是 Cloudflare 适配层（自动生成，勿手改；改业务逻辑请改 server.mjs 后重跑 build.py）
// ════════════════════════════════════════════════════════════════════════
import { ASSETS, GLOSSARY_TEXT, ORAL_TEXT } from "./assets.js";
import { currentUser } from "../shared/session.js";

/** 每次请求开始前由 fetch 入口填进来（Worker 没有全局 env） */
let LIVE_ENV = {};
let LIVE_CTX = null;
let LIVE_USER = null;      // 本次请求的登录用户（用于计费）

/** 兜底去重：同一段里出现高度重复的两句时，只保留第一句（字符二元组相似度 ≥0.62 视为重复） */
function dedupeSentences(text) {
  const src = String(text || "").trim();
  if (!src) return src;
  // 中英文句末标点都要分句（原写法漏了英文句点，导致英文重复句永远去不掉）
  const parts = src.split(/(?<=[。！？!?；;\.\n])/).map((x) => x.trim()).filter(Boolean);
  if (parts.length < 2) return src;
  const grams = (t) => {
    const clean = t.replace(/[\s，。！？、；：""''（）()\[\]【】·…—]/g, "");
    const set = new Set();
    for (let i = 0; i < clean.length - 1; i += 1) set.add(clean.slice(i, i + 2));
    return set;
  };
  const kept = [];
  const keptGrams = [];
  for (const p of parts) {
    const g = grams(p);
    if (g.size >= 5) {   // 太短的句子不去重（可能是正常的重复强调）
      let dup = false;
      for (const kg of keptGrams) {
        let inter = 0;
        for (const x of g) if (kg.has(x)) inter += 1;
        const sim = inter / (g.size + kg.size - inter);
        if (sim >= 0.45) { dup = true; break; }
      }
      if (dup) continue;
    }
    kept.push(p);
    keptGrams.push(g);
  }
  return kept.join("");
}

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
'''

FETCH_TAIL = r'''
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
'''


def transform(src: str) -> str:
    # 1) 去掉 Node 专有 import
    src = re.sub(r"^import http from 'node:http';\n", "", src, flags=re.M)
    src = re.sub(r"^import \{ readFile \} from 'node:fs/promises';\n", "", src, flags=re.M)
    src = re.sub(r"^import path from 'node:path';\n", "", src, flags=re.M)
    src = re.sub(r"^import \{ fileURLToPath \} from 'node:url';\n", "", src, flags=re.M)
    src = re.sub(r"^import \{ createRequire \} from 'node:module';\n", "", src, flags=re.M)
    src = re.sub(r"^const nodeRequire = createRequire\(import\.meta\.url\);\n", "", src, flags=re.M)
    src = re.sub(r"^const \{ existsSync, readFileSync, writeFileSync \} = nodeRequire\('node:fs'\);\n", "", src, flags=re.M)

    # 2) 模块级常量 → 每请求读取的函数
    src = re.sub(r"^const MOCK = .*$", "function MOCKF() { return /^(1|true|yes)$/i.test(PE('MOCK', '') || ''); }", src, flags=re.M)
    src = re.sub(r"const ENV = \{[^}]*\};",
                 """function ENVF() {
  return {
    baseUrl: String(PE('AI_BASE_URL', '')).trim().replace(/\\/+$/, ''),
    apiKey: String(PE('AI_API_KEY', '')).trim(),
    chatModel: String(PE('CHAT_MODEL', 'deepseek-chat')).trim(),
    ttsModel: String(PE('TTS_MODEL', 'tts-1')).trim(),
    ttsVoice: String(PE('TTS_VOICE', 'nova')).trim(),
    sttModel: String(PE('STT_MODEL', 'whisper-1')).trim(),
  };
}""", src, count=1, flags=re.S)
    src = src.replace("const PROFILES = { ...loadProfilesSync() };", "")
    # 保留原版的归一化逻辑（它会跳过 _说明 这类非对象项），只是改成"首次请求时读一次"
    src = re.sub(r"^const PROFILES = loadProfilesSync\(\);$",
                 "let __profilesCache = null;\nfunction PROFILESF() { if (!__profilesCache) __profilesCache = loadProfilesSync(); return __profilesCache; }",
                 src, flags=re.M)

    # 引用点替换（先长后短，避免误伤）
    src = re.sub(r"\bENV\.", "ENVF().", src)
    src = re.sub(r"\bprocess\.env\.([A-Z0-9_]+)", r"PE('\1')", src)
    src = re.sub(r"(?<![A-Za-z0-9_.'\"])MOCK(?![A-Za-z0-9_])", "MOCKF()", src)
    src = re.sub(r"(?<![A-Za-z0-9_.])PROFILES(?![A-Za-z0-9_])", "PROFILESF()", src)
    src = src.replace("MOCKF()F()", "MOCKF()")  # 防重复替换
    src = src.replace("PROFILESF()F()", "PROFILESF()")

    # 2.5) Cloudflare 不允许模块顶层的定时器/异步 I/O；用量改为进程内保存
    src = re.sub(r"^const \w+ = setInterval\(.*$", "// （Cloudflare 版）用量改为实例内存保存，不做定时落盘", src, flags=re.M)
    src = re.sub(r"^if \(\w+Timer\.unref\).*$", "", src, flags=re.M)

    # 3) 入口改造
    src = src.replace("const server = http.createServer(async (req, res) => {", "async function handleNodeRequest(req, res) {")
    i = src.find("server.listen(PORT, HOST")
    if i > 0:
        src = src[:i].rstrip().rstrip(";") + "\n"
    # 原写法是 const server = http.createServer(async (req,res) => { ... });  结尾残留一个 "})"
    src = re.sub(r"\n\}\)\s*$", "\n}", src, count=1)
    # 去掉 const server = 那行的残留写法（有的版本是 http.createServer(...) 直接赋值）
    src = re.sub(r"^const server = http\.createServer\(async \(req, res\) => \{$", "async function handleNodeRequest(req, res) {", src, flags=re.M)

    # 4) 静态托管无需改动：readFile 适配器会在运行时返回内置资源

    return PRELUDE + "\n" + src + "\n" + FETCH_TAIL


def main() -> int:
    if not os.path.isfile(SRC):
        print("找不到源文件：%s" % SRC, file=sys.stderr)
        return 1
    src = read(SRC)
    out = transform(src)

    with open(os.path.join(HERE, "assets.js"), "w", encoding="utf-8") as fh:
        fh.write(gen_assets())
    with open(os.path.join(HERE, "worker.js"), "w", encoding="utf-8") as fh:
        fh.write("// 由 cf/live/build.py 从 live-interpreter/server.mjs 自动生成，请勿手改\n")
        fh.write(out)

    lines = out.count("\n")
    print("已生成 cf/live/worker.js（%d 行）与 cf/live/assets.js" % lines)
    # 自检：不该再出现 Node 专有写法
    for bad in ("node:http", "node:fs", "http.createServer(", "server.listen(", "readFile(PUBLIC"):
        if bad in out:
            print("⚠️ 仍含 %s —— 需要检查转换规则" % bad, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
