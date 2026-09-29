/**
 * aurora-translate 的云端中转层（Cloudflare Worker）
 *
 * 它解决三件事：
 *   1. 密码门     —— 只有拿到口令的人能用，翻译的钱（你的 API key）不会被陌生人消耗
 *   2. 大文件     —— GitHub Issue 附件上限 25MB，这里把原件和译文存进 Cloudflare，
 *                    单个文件到 95MB，而且**不需要绑银行卡**
 *   3. 进度与下载 —— 页面实时看进度，译完直接点下载，不用去 GitHub 翻
 *
 * 存储后端（自动选）：
 *   * 没绑 R2  →  Workers KV 分块（免费额度 1GB 存储、每天 1000 次写、10 万次读，
 *                 单值上限 25MiB，所以按 20MiB 切块；靠 TTL 自动过期，不花你的钱）
 *   * 绑了 R2  →  直接用 R2（同一套接口，代码一行都不用改）
 *
 * 文件流：
 *   浏览器 --PUT /api/upload--> 存储层 --dispatch--> GitHub Actions
 *   Actions --GET /api/input/<id>--> 取原件
 *   Actions --POST /api/result/<id>--> 回传译文
 *   Actions --POST /api/report/<id>--> 回传进度/统计
 *   浏览器 --GET /api/status?id=--> 看进度；--GET /api/download?id=--> 下载
 */

import { PAGE } from "./page.js";
// 与账户站共用同一份会话/额度逻辑，并读同一个 D1（阶段 2）
import { currentUser, quotaState, addUsage, canRefine } from "./shared/session.js";

const MAX_UPLOAD = 95 * 1024 * 1024; // Workers 免费版请求体上限 100MB，留点余量
const CHUNK = 20 * 1024 * 1024; // KV 单值上限 25MiB，切 20MiB 一块最稳
const BLOB_TTL = 3 * 24 * 3600; // 原件与译文保留 3 天（KV 只有 1GB，靠 TTL 回收）
const JOB_TTL = 7 * 24 * 3600; // 任务记录保留 7 天，历史里还能看到
const DAILY_JOBS = 30; // 每天最多接这么多任务
const DAILY_BYTES = 250 * 1024 * 1024; // 每天最多收这么多字节（3 天滚动也在 1GB 之内）
const MODES = new Set(["inplace", "bilingual", "ocr"]);
const TARGETS = new Set(["中文", "英文", "日文", "韩文", "法文", "德文", "西班牙文", "俄文"]);

const MODE_LABEL = { inplace: "保持版式", bilingual: "中英对照", ocr: "扫描件 OCR" };

const COMMUNITY_LIMIT = 50; // 公共文件区最多列这么多条
const COMMUNITY_SCAN = 500; // 最多扫这么多条任务记录（KV 读次数要省着用，别把 CPU 打满）

/**
 * 自动化冒烟测试的任务名长这样：smoke-test.pdf / smoke-refine.pdf / smoke-refine2.pdf …
 *
 * 为什么按"前缀"认而不是列一串完整文件名：名字是自检脚本（.github/workflows/smoke.yml）
 * 自己起的，加一个用例就多一个名字，硬编码一份清单迟早漏。前缀是那串名字唯一稳定的共同点；
 * 顺带放宽到 smoke_ 和大小写，免得哪天脚本换个写法就漏网。
 * 真实用户上传的文件叫人名/课件名，不会以 smoke- 开头，误伤的概率可以忽略。
 */
const SMOKE_NAME_RE = /^smoke[-_]/i;

/**
 * 这是不是自动化/自检产生的任务（公共文件区要把它挡在外面）。
 *
 * 两条信号取"或"，互为兜底：
 *   ① 名字以 smoke- / smoke_ 开头 —— 现在的自检脚本就是这么起名的；
 *   ② 任务没有 userId —— 上传走的是管理口令通道（只有自动化与应急才会这么干），
 *      真人从网页登录上传的任务一定带 userId（见 handleUpload 里的 userId 赋值）。
 * 光靠①，脚本哪天换个文件名就漏进来了；光靠②，管理员手动用口令传的文件也会被当成自检。
 * 合起来才稳。注意②不会误伤真人：正常登录用户的任务永远有 userId。
 */
function isAutomationJob(job) {
  if (SMOKE_NAME_RE.test(String((job && job.name) || "").trim())) return true;
  return !(job && job.userId);
}

/**
 * 邮箱脱敏：wanga@qq.com → wa***@qq.com
 *
 * 为什么留前两位而不是全打码：公共区里得让人认得出"这是我同事传的"，不然一堆 ***@qq.com
 * 谁也不敢下；但完整邮箱摊在网上，被扫一遍就是一个通讯录，所以只留个"对得上号"的头。
 */
function maskEmail(email) {
  const s = String(email || "").trim();
  const at = s.lastIndexOf("@");
  if (at <= 0) return "";                      // 空邮箱（口令通道传的任务）就空着，页面显示占位符
  const local = s.slice(0, at);
  return local.slice(0, local.length >= 2 ? 2 : 1) + "***" + s.slice(at);
}

/** 当前请求的角色：管理口令通道按管理员算（和 ownedBy 的既有口径保持一致） */
function authRole(auth) {
  if (auth.admin) return "admin";
  return auth.user ? String(auth.user.role || "") : "";
}

/** 角色给页面看的中文名，报错信息里说人话 */
const ROLE_LABEL = { user: "普通用户", vip: "VIP", admin: "管理员" };

const storeKind = (env) => (env.FILES ? "r2" : "kv");

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function fail(message, status = 400) {
  return json({ ok: false, error: message }, status);
}

function jobKey(id) {
  return "job:" + id;
}

/**
 * 请求头里的中文：网页端会 encodeURIComponent（HTTP 头只能是 ASCII），
 * 命令行/curl 往往直接发原文。两种都得认 —— 解不开就按原样返回，绝不抛错。
 */
function decodeHeader(value, fallback = "") {
  if (!value) return fallback;
  try {
    return decodeURIComponent(value);
  } catch (err) {
    return value;
  }
}

function humanSize(bytes) {
  if (!bytes) return "0";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

// ── 存储层：对外只有 putBlob / readBlob / delBlob 三个动作 ────────────────
// kind 取 "inputs" 或 "results"，一个任务各一份。

/** 把一条流写进存储，返回 {count, size}；KV 走分块，R2 就是单个对象 */
async function putBlob(env, id, kind, body) {
  if (storeKind(env) === "r2") {
    await env.FILES.put(kind + "/" + id + "/blob", body, {
      httpMetadata: { contentType: "application/octet-stream" },
    });
    return { count: 1, size: null };
  }

  const reader = body.getReader();
  let pieces = [];
  let size = 0; // 当前累计（还没落盘）
  let total = 0; // 整个文件
  let index = 0;

  const flush = async () => {
    if (!size) return;
    const chunk = new Uint8Array(size);
    let off = 0;
    for (const p of pieces) {
      chunk.set(p, off);
      off += p.length;
    }
    await env.JOBS.put("blob:" + kind + ":" + id + ":" + index, chunk, { expirationTtl: BLOB_TTL });
    index += 1;
    pieces = [];
    size = 0;
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value || !value.length) continue;
    total += value.length;
    if (total > MAX_UPLOAD) {
      await reader.cancel().catch(() => {});
      await delBlob(env, id, kind, index + 1);
      throw new Error("文件超过上限 " + humanSize(MAX_UPLOAD));
    }
    pieces.push(value);
    size += value.length;
    if (size >= CHUNK) await flush();
  }
  await flush();
  return { count: index, size: total };
}

/** 读回一条流；count 是写入时得到的块数（R2 模式下忽略） */
async function readBlob(env, id, kind, count) {
  if (storeKind(env) === "r2") {
    const head = await env.FILES.head(kind + "/" + id + "/blob");
    if (!head) return null;
    const obj = await env.FILES.get(kind + "/" + id + "/blob");
    return { stream: obj.body, size: head.size };
  }
  if (!count) return null;
  const prefix = "blob:" + kind + ":" + id + ":";
  const first = await env.JOBS.get(prefix + "0", "arrayBuffer");
  if (!first) return null;

  let i = 1;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(first));
    },
    async pull(controller) {
      if (i >= count) {
        controller.close();
        return;
      }
      const buf = await env.JOBS.get(prefix + i, "arrayBuffer");
      i += 1;
      if (!buf) {
        controller.close();
        return;
      }
      controller.enqueue(new Uint8Array(buf));
    },
  });
  return { stream, size: null };
}

async function delBlob(env, id, kind, count) {
  if (storeKind(env) === "r2") {
    await env.FILES.delete(kind + "/" + id + "/blob").catch(() => {});
    return;
  }
  for (let i = 0; i < Math.max(count, 1); i += 1) {
    await env.JOBS.delete("blob:" + kind + ":" + id + ":" + i).catch(() => {});
  }
}

// ── 任务记录 ──────────────────────────────────────────────────────────

async function readJob(env, id) {
  return env.JOBS.get(jobKey(id), "json");
}

async function saveJob(env, job) {
  await env.JOBS.put(jobKey(job.id), JSON.stringify(job), { expirationTtl: JOB_TTL });
  return job;
}

/** 每日配额：免费额度是 1GB 存储，用这个挡住"口令泄露后被人灌爆" */
async function quotaToday(env) {
  const day = new Date().toISOString().slice(0, 10);
  const rec = (await env.JOBS.get("quota:" + day, "json")) || { jobs: 0, bytes: 0 };
  return { day, rec };
}

async function quotaCheck(env, bytes) {
  const { rec } = await quotaToday(env);
  if (rec.jobs >= DAILY_JOBS) return "今天任务已排满（上限 " + DAILY_JOBS + " 个），明天再来";
  if (rec.bytes + bytes > DAILY_BYTES) {
    return "今天上传总量已到上限（" + humanSize(DAILY_BYTES) + "），明天再来";
  }
  return null;
}

async function quotaAdd(env, bytes) {
  const { day, rec } = await quotaToday(env);
  rec.jobs += 1;
  rec.bytes += bytes;
  await env.JOBS.put("quota:" + day, JSON.stringify(rec), { expirationTtl: 3 * 24 * 3600 });
}

// ── GitHub ───────────────────────────────────────────────────────────

async function ghFetch(env, path, init = {}) {
  const headers = {
    authorization: "Bearer " + env.GH_TOKEN,
    accept: "application/vnd.github+json",
    "user-agent": "aurora-translate-worker",
    "x-github-api-version": "2022-11-28",
    ...(init.headers || {}),
  };
  return fetch("https://api.github.com/repos/" + env.GH_REPO + path, { ...init, headers });
}

/**
 * 把 Actions 里正在跑的那一步翻译成"人话"，让人一眼知道现在在干嘛。
 * 顺序有讲究：先匹配更具体的（"安装系统" 要在 "翻译" 之前判）。
 */
const PHASE_RULES = [
  ["安装系统", "正在准备翻译环境（装 OCR 引擎与中文字体，首次约 1~2 分钟）"],
  ["安装 Python", "正在安装翻译依赖"],
  ["取原件", "正在取回你的文件"],
  ["开始翻译", "正在启动翻译"],
  ["回传译文", "正在回传译文"],
  ["回传统计", "正在收尾"],
  ["翻译", "正在翻译正文（最耗时的一步）"],
  ["setup-python", "正在准备运行环境"],
  ["checkout", "正在准备运行环境"],
  ["Set up job", "正在分配运行机器"],
  ["Complete job", "正在收尾"],
];

function phaseFor(stepName) {
  if (!stepName) return "";
  for (const [needle, text] of PHASE_RULES) {
    if (stepName.includes(needle)) return text;
  }
  return stepName;
}

/**
 * GitHub 上这次运行的"事实"，带 8 秒内存缓存（只在当前实例里，best-effort）。
 *
 * ★ 为什么坚决不写回 KV：KV 是跨机房最终一致的。曾经这里是"读记录→改→写回"，
 *   结果一个还没同步到"完成"的机房把旧状态写回去，**覆盖掉了 Actions 已经写好的
 *   resultChunks，好任务被标成失败**，而且它反复续命、永远收敛不了。
 *   所以：派生状态一律只用于本次响应，绝不落库；KV 里只保留 Actions 明确上报的状态。
 */
const RUN_CACHE = new Map();
const RUN_TTL = 8000;

async function runFacts(env, runId) {
  const hit = RUN_CACHE.get(runId);
  if (hit && Date.now() - hit.at < RUN_TTL) return hit;

  const facts = {
    at: Date.now(), status: "", conclusion: "", phase: "", stepIndex: 0, stepTotal: 0, url: "",
  };
  try {
    const resp = await ghFetch(env, "/actions/runs/" + runId);
    if (resp.ok) {
      const run = await resp.json();
      facts.status = run.status || "";
      facts.conclusion = run.conclusion || "";
      facts.url = run.html_url || "";
    }
    if (facts.status !== "completed") {
      const jobsResp = await ghFetch(env, "/actions/runs/" + runId + "/jobs");
      if (jobsResp.ok) {
        const data = await jobsResp.json();
        const list = data.jobs || [];
        const target = list.find((j) => j.name === "cloud") || list[0];
        if (target) {
          const steps = target.steps || [];
          const running = steps.findIndex((s) => s.status === "in_progress");
          const doneCount = steps.filter((s) => s.status === "completed").length;
          facts.stepTotal = steps.length;
          facts.stepIndex = running >= 0 ? running + 1 : Math.max(doneCount, 1);
          if (running >= 0) facts.phase = phaseFor(steps[running].name);
          else if (target.status === "queued") facts.phase = "已排队，等 GitHub 分配机器…";
        }
      }
    }
  } catch (err) {
    // 查不到就用记录里的状态，不影响主流程
  }
  if (RUN_CACHE.size > 50) RUN_CACHE.clear();   // 实例长活时别无限长大
  RUN_CACHE.set(runId, facts);
  return facts;
}

/** 这个机房现在能不能读到译文块（KV 跨机房会有延迟，读得到才算真的能下载） */
async function resultVisible(env, id, chunks) {
  if (storeKind(env) === "r2") {
    return (await env.FILES.head("results/" + id + "/blob")) !== null;
  }
  if (!chunks) return false;
  const first = await env.JOBS.get("blob:results:" + id + ":0", "arrayBuffer");
  return !!first;
}

/** 译文块到底在不在（记录可能被搞乱，但数据不会骗人）——用于自愈与下载兜底 */
async function resultChunkCount(env, id) {
  if (storeKind(env) === "r2") {
    const head = await env.FILES.head("results/" + id + "/blob");
    return head ? 1 : 0;
  }
  let n = 0;
  for (let i = 0; i < 40; i += 1) {
    const buf = await env.JOBS.get("blob:results:" + id + ":" + i, "arrayBuffer");
    if (!buf) break;
    n = i + 1;
  }
  return n;
}

/** workflow_dispatch 不返回 run id，用 run-name 里带的 job_id 去列表里认领 */
async function adoptRun(env, jobId) {
  for (let attempt = 0; attempt < 6; attempt++) {
    await new Promise((r) => setTimeout(r, 1500));
    const resp = await ghFetch(
      env,
      "/actions/workflows/" + (env.WORKFLOW || "translate.yml") + "/runs?event=workflow_dispatch&per_page=10",
    );
    if (!resp.ok) return;
    const data = await resp.json();
    const run = (data.workflow_runs || []).find((r) => (r.display_title || "").includes(jobId));
    if (!run) continue;
    const job = await readJob(env, jobId);
    if (!job || job.status === "failed") return;
    job.runId = run.id;
    job.runUrl = run.html_url;
    await saveJob(env, job);
    return;
  }
}

// ── 鉴权 ─────────────────────────────────────────────────────────────

/**
 * 鉴权（阶段 2 起）：
 *   ① 登录会话 —— 正常路径，读取 D1 里的会话，顺带核对本月额度
 *   ② 管理口令 —— 自动化线上自检与应急通道（不占任何人额度）
 * 返回 { user } / { admin: true } / { error }
 *
 * opts.skipQuota：跳过"本月额度用完"这道门。
 *   为什么需要：看/下别人已经译好的成品不花一分钱 API 额度，被 402 拦住没有道理。
 *   默认 false —— 上传、查进度、看历史的额度判定一个字节都不变。
 */
async function authorizeUser(request, env, url, opts = {}) {
  if (env.DB) {
    try {
      const user = await currentUser(env, request);
      if (user) {
        if (opts.skipQuota) return { user };
        const q = quotaState(user);
        if (!q.unlimited && q.remaining <= 0) {
          return { user, error: `本月额度已用完（已用 ${q.used}/${q.quota} 页），下个月自动恢复，或找管理员加额度` };
        }
        return { user };
      }
    } catch (err) {
      // 数据库暂时读不到就退到口令通道，别把服务卡死
    }
  }
  const given = request.headers.get("x-password") || url.searchParams.get("password") || "";
  if (env.PASSWORD && (given === env.PASSWORD || decodeHeader(given) === env.PASSWORD)) {
    return { admin: true };
  }
  return { error: "请先登录后再使用（没有账号？去 account.ourmetaverse.cn 注册，第一个账号是管理员）" };
}

/** 任务归属校验：不是自己的任务不给看（管理员例外） */
function ownedBy(job, auth) {
  if (!job.userId) return true;              // 口令通道创建的任务
  if (!auth.user) return false;
  return auth.user.id === job.userId || auth.user.role === "admin";
}

/**
 * 能不能读"别人的"任务产出。管理员一直可以（原有口径），VIP 从公共文件区起也可以。
 *
 * ★ 只管下载/列表，不管 /api/status：状态里带 runId、phase、note、GitHub 运行链接，
 *   是"这个任务此刻在干嘛"的过程细节，公共区只展示成品，没有理由把别人的过程摊开。
 */
function canReadOthers(auth) {
  const role = authRole(auth);
  return role === "admin" || role === "vip";
}

function authorizeAgent(request, env) {
  if (!env.AGENT_KEY) return "服务端还没设置 AGENT_KEY";
  if (request.headers.get("x-agent-key") !== env.AGENT_KEY) return "内部密钥不对";
  return null;
}

// ── 接口 ─────────────────────────────────────────────────────────────

async function handleUpload(request, env, ctx) {
  const auth = await authorizeUser(request, env, new URL(request.url));
  if (auth.error) return fail(auth.error, auth.user ? 402 : 401);

  const name = decodeHeader(request.headers.get("x-filename"));
  if (!name) return fail("没收到文件名");
  const lower = name.toLowerCase();
  if (!lower.endsWith(".pdf") && !lower.endsWith(".docx") && !lower.endsWith(".doc")) {
    return fail("只支持 PDF 和 Word（.pdf / .docx）");
  }

  const mode = decodeHeader(request.headers.get("x-mode"), "inplace");
  if (!MODES.has(mode)) return fail("输出形式不对：" + mode);
  const target = decodeHeader(request.headers.get("x-target"), "中文");
  if (!TARGETS.has(target)) return fail("目标语言不对：" + target);

  // 精修：前端说了不算，只认账号上的能力位（can_refine）。
  // 没有权限就**静默降级成 0**，不报错 —— 老客户端/手写请求带了这个头也不该把上传打断。
  const refine = (request.headers.get("x-refine") === "1" && canRefine(auth.user)) ? 1 : 0;

  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > MAX_UPLOAD) {
    return fail("文件太大（" + humanSize(declared) + "），当前上限 " + humanSize(MAX_UPLOAD), 413);
  }
  if (!request.body) return fail("没收到文件内容");

  const quotaErr = await quotaCheck(env, declared);
  if (quotaErr) return fail(quotaErr, 429);

  const id = crypto.randomUUID().replace(/-/g, "").slice(0, 20);
  let written;
  try {
    written = await putBlob(env, id, "inputs", request.body);
  } catch (err) {
    return fail("保存文件失败：" + (err && err.message ? err.message : String(err)), 500);
  }
  const size = written.size || declared;
  await quotaAdd(env, size);

  const job = {
    id,
    name,
    mode,
    target,
    refine,                                        // 1 = 初译之上再用精修模型打磨一遍
    size,
    status: "queued",
    note: "已收到，正在排队",
    createdAt: Date.now(),
    backend: storeKind(env),
    inputChunks: written.count,
    inputSize: size,
    source: "cloud",
    userId: auth.user ? auth.user.id : null,      // 完成后按页扣他的额度
    userEmail: auth.user ? auth.user.email : "",
  };
  await saveJob(env, job);

  const resp = await ghFetch(
    env,
    "/actions/workflows/" + (env.WORKFLOW || "translate.yml") + "/dispatches",
    {
      method: "POST",
      body: JSON.stringify({
        ref: env.GH_REF || "main",
        inputs: { job_id: id, mode, target, refine: refine ? "1" : "0" },
      }),
    },
  );
  if (!resp.ok) {
    const text = await resp.text();
    job.status = "failed";
    job.note = "没能启动 GitHub 任务（HTTP " + resp.status + "）：" + text.slice(0, 200);
    await saveJob(env, job);
    return fail(job.note, 502);
  }

  ctx.waitUntil(adoptRun(env, id).catch(() => {}));
  return json({ ok: true, id, name, mode, target, status: job.status });
}

/** 记录说没完成、但译文块确实在 → 以数据为准，把记录修好（幂等，只升不降） */
async function healJob(env, job) {
  if (job.status === "done") return job;
  const chunks = await resultChunkCount(env, job.id);
  if (!chunks) return job;
  job.status = "done";
  job.resultChunks = chunks;
  job.resultName = job.resultName || defaultResultName(job);
  job.note = "翻译完成";
  job.doneAt = job.doneAt || Date.now();
  return saveJob(env, job);
}

/** 记录没坏时的正常命名：<原名>_<模式>.<扩展名>（和命令行一致） */
function defaultResultName(job) {
  const name = job.name || "input.pdf";
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : ".pdf";
  return stem + "_" + (job.mode || "inplace") + ext;
}

async function handleStatus(request, env, url) {
  const auth = await authorizeUser(request, env, url);
  if (auth.error) return fail(auth.error, 401);
  const id = url.searchParams.get("id");
  if (!id) return fail("缺少 id");
  let job = await readJob(env, id);
  if (!job) return fail("没有这个任务（记录保留 7 天）", 404);
  if (!ownedBy(job, auth)) return fail("这个任务不属于当前账号", 403);
  job = await healJob(env, job);

  // 以下都是"派生状态"：只用于这次响应，**不写回 KV**（写回会覆盖 Actions 的真实上报）
  let status = job.status;
  let phase = "";
  let note = job.note || "";
  let stepIndex = 0;
  let stepTotal = 0;
  let runUrl = job.runUrl || "";

  if (job.runId && status !== "done") {
    const facts = await runFacts(env, job.runId);
    if (facts.url) runUrl = facts.url;
    if (facts.status === "completed") {
      if (facts.conclusion === "success") {
        status = "running";
        phase = "翻译已完成，译文正在同步（约 1 分钟）…";
      } else if (status !== "failed") {
        status = "failed";
        phase = facts.conclusion === "cancelled" ? "任务被取消" : "翻译失败";
        note = facts.conclusion === "cancelled"
          ? "任务被取消"
          : "翻译失败（" + facts.conclusion + "），常见原因：文件加密、语言不支持、额度用尽";
      }
    } else {
      status = facts.status === "in_progress" ? "running" : "queued";
      phase = facts.phase || (status === "queued" ? "已排队，马上开始…" : "正在翻译…");
      stepIndex = facts.stepIndex;
      stepTotal = facts.stepTotal;
    }
  }
  // ★ 别急着说"完成"：任务记录可能已经同步过来，但译文分块还在路上（KV 跨机房延迟）。
  //   这里先探一下，这个机房读不到就继续显示"同步中"，免得用户点了下载却拿到报错。
  if (status === "done" && !(await resultVisible(env, job.id, job.resultChunks))) {
    // ★ 必须有上限：resultChunks 缺失（老记录/记录被写坏）时 resultVisible 恒为 false，
    //   而任务其实早就完成了 —— 会永远显示"正在同步"，线上真卡过一次 15 分钟。
    //   完成超过 2 分钟还看不到块，就照实报完成，让用户去点下载（下载端自己会等/会报错）。
    const doneAt = Number(job.doneAt || job.createdAt || 0);
    if (!doneAt || Date.now() - doneAt < 120000) {
      status = "running";
      phase = "译文正在同步（约 1 分钟）…";
    }
  }
  if (status === "done") phase = "翻译完成";
  else if (status === "failed") phase = phase || note || "翻译失败";
  else if (!phase) phase = status === "queued" ? "已排队，马上开始…" : "正在翻译…";

  return json({
    ok: true,
    id: job.id,
    name: job.name,
    mode: job.mode,
    modeLabel: MODE_LABEL[job.mode] || job.mode,
    target: job.target,
    refine: job.refine ? 1 : 0,
    size: job.size,
    status,
    // phase 是"给人看的当前阶段"，note 是更细的说明
    phase,
    note,
    stepIndex,
    stepTotal,
    // 从提交到现在过了多久（页面自己再往上加秒数，避免频繁请求）
    elapsedSec: Math.max(0, Math.round((Date.now() - (job.createdAt || Date.now())) / 1000)),
    runUrl,
    ready: status === "done",
    stats: job.stats || null,
  });
}

async function handleDownload(request, env, url) {
  // skipQuota：下载已经译好的成品不再消耗额度，别让"额度用完了"拦着人取自己的文件
  const auth = await authorizeUser(request, env, url, { skipQuota: true });
  if (auth.error) return fail(auth.error, 401);
  const id = url.searchParams.get("id");
  let job = await readJob(env, id);
  if (!job) return fail("没有这个任务", 404);
  // 自己的一直能下；VIP/管理员还能下公共文件区里别人的成品（见 canReadOthers 的说明）
  if (!ownedBy(job, auth) && !canReadOthers(auth)) return fail("这个任务不属于当前账号", 403);
  // 记录说没完成也不算数：先去存储里看译文块在不在（跨机房延迟会把记录写乱）
  job = await healJob(env, job);
  if (!job.resultChunks) return fail("译文还没好", 409);

  let blob = await readBlob(env, id, "results", job.resultChunks);
  // 跨机房同步有延迟：等最多 12 秒再下结论
  for (let i = 0; i < 4 && !blob; i += 1) {
    await new Promise((r) => setTimeout(r, 3000));
    blob = await readBlob(env, id, "results", job.resultChunks);
  }
  if (!blob) {
    const ageSec = (Date.now() - (job.createdAt || 0)) / 1000;
    if (ageSec > BLOB_TTL) return fail("译文已过期（原件与译文只保留 3 天）", 410);
    return fail("译文正在同步，请 10 秒后再点一次下载", 409);
  }
  const filename = job.resultName || "translated.pdf";
  const ext = (filename.match(/\.[a-z0-9]+$/i) || [".pdf"])[0];
  const headers = {
    "content-type": "application/octet-stream",
    "content-disposition":
      'attachment; filename="translated' + ext + "\"; filename*=UTF-8''" + encodeURIComponent(filename),
    "cache-control": "no-store",
  };
  const size = blob.size || job.resultSize;
  if (size) headers["content-length"] = String(size);
  return new Response(blob.stream, { headers });
}

async function handleInput(request, env) {
  const bad = authorizeAgent(request, env);
  if (bad) return fail(bad, 401);
  const id = new URL(request.url).pathname.split("/").pop();
  const job = await readJob(env, id);
  if (!job) return fail("没有这个任务", 404);
  const blob = await readBlob(env, id, "inputs", job.inputChunks);
  if (!blob) return fail("原件不见了", 404);
  return new Response(blob.stream, {
    headers: {
      "content-type": "application/octet-stream",
      "x-filename": encodeURIComponent(job.name),
      "x-mode": job.mode,
      "x-target": encodeURIComponent(job.target),
    },
  });
}

async function handleResult(request, env) {
  const bad = authorizeAgent(request, env);
  if (bad) return fail(bad, 401);
  const id = new URL(request.url).pathname.split("/").pop();
  const job = await readJob(env, id);
  if (!job) return fail("没有这个任务", 404);
  if (!request.body) return fail("没收到译文内容");

  const resultName = decodeHeader(request.headers.get("x-filename"), job.name);
  let written;
  try {
    written = await putBlob(env, id, "results", request.body);
  } catch (err) {
    return fail("保存译文失败：" + (err && err.message ? err.message : String(err)), 500);
  }

  job.resultName = resultName;
  job.resultChunks = written.count;
  job.resultSize = written.size || Number(request.headers.get("content-length") || 0);
  job.status = "done";
  job.note = "翻译完成";
  job.doneAt = Date.now();
  await saveJob(env, job);
  return json({ ok: true });
}

async function handleReport(request, env) {
  const bad = authorizeAgent(request, env);
  if (bad) return fail(bad, 401);
  const id = new URL(request.url).pathname.split("/").pop();
  const job = await readJob(env, id);
  if (!job) return fail("没有这个任务", 404);
  const body = await request.json().catch(() => ({}));
  if (body.status && ["queued", "running", "done", "failed"].includes(body.status)) {
    // 只升不降：done 之后迟到的 running/queued 一律忽略，避免把好记录写坏
    if (job.status !== "done" || body.status === "done") {
      job.status = body.status;
    }
  }
  if (body.note) job.note = String(body.note).slice(0, 500);
  if (body.stats) job.stats = body.stats;

  // 按页扣额度并写用量流水（幂等：同一个任务只记一次）
  if (job.userId && !job.usageRecorded && job.stats && Number(job.stats.pages) >= 0) {
    try {
      const pages = Number(job.stats.pages) || 0;
      // 精修要跑两遍模型（初译 + 打磨），所以按 1.5 倍计页；向上取整，不足 1 页也算 1 页。
      // 上传时的预检仍按实际页数，这里才是真正扣账的地方。
      const billed = job.refine ? Math.ceil(pages * 1.5) : pages;
      await addUsage(env, job.userId, {
        jobId: job.id,
        pages: billed,
        tokensIn: job.stats.api_tokens_in,
        tokensOut: job.stats.api_tokens_out,
        note: `${job.mode || ""} · ${job.name || ""}${job.refine ? " · 精修" : ""}`,
      });
      job.usageRecorded = true;
      job.billedPages = billed;
    } catch (err) {
      // 记账失败不让任务失败，下轮状态查询还会再试
    }
  }
  await saveJob(env, job);
  return json({ ok: true });
}

/** 页面用：当前登录状态 + 剩余额度（未登录不算错误，返回 logged_in:false） */
async function handleMe(request, env) {
  if (!env.DB) return json({ ok: true, logged_in: false, note: "未绑定账户库" });
  let user = null;
  try {
    user = await currentUser(env, request);
  } catch (err) {
    user = null;
  }
  if (!user) return json({ ok: true, logged_in: false });
  const q = quotaState(user);
  return json({
    ok: true,
    logged_in: true,
    user: { email: user.email, role: user.role, can_refine: canRefine(user) ? 1 : 0 },
    quota: {
      unlimited: q.unlimited,
      quota: q.quota,
      used: q.used,
      remaining: q.unlimited ? -1 : q.remaining,
    },
  });
}

async function handleHistory(request, env, url) {
  const auth = await authorizeUser(request, env, url);
  if (auth.error) return fail(auth.error, 401);
  const list = await env.JOBS.list({ prefix: "job:", limit: 100 });
  const jobs = [];
  const mine = auth.user ? auth.user.id : null;
  const isAdmin = auth.admin || (auth.user && auth.user.role === "admin");
  for (const key of list.keys) {
    const job = await env.JOBS.get(key.name, "json");
    if (!job) continue;
    if (!isAdmin && (job.userId || null) !== mine) continue;   // 只看自己的
    jobs.push({
      id: job.id,
      name: job.name,
      modeLabel: MODE_LABEL[job.mode] || job.mode,
      status: job.status,
      note: job.note || "",
      refine: job.refine ? 1 : 0,
      createdAt: job.createdAt,
      ready: job.status === "done",
      target: job.target,
    });
  }
  jobs.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  return json({ ok: true, jobs: jobs.slice(0, 30) });
}

/**
 * 公共文件区：VIP 及以上能看到/下载"别人译好的成品"。
 *
 * ★ 为什么另开一个接口，而不是给 /api/history 加个参数：
 *   history 是"我的任务"，里面有进行中、失败的条目和 note；公共区只该有做好的成品。
 *   两者语义、权限都不同，混在一个接口里，早晚会顺手把别人的过程细节漏出去。
 *
 * ★ 为什么排除自动化产物：.github/workflows/smoke.yml 每次线上自检都会真上传一个样例文件，
 *   它跟用户文档混在一起会让公共区变成"测试垃圾场"。判断见 isAutomationJob。
 */
async function handleCommunity(request, env, url) {
  // skipQuota：看列表不花额度，额度用完的 VIP 也该能逛（下载同理，见 handleDownload）
  const auth = await authorizeUser(request, env, url, { skipQuota: true });
  if (auth.error) return fail(auth.error, 401);
  const role = authRole(auth);
  if (role !== "vip" && role !== "admin") {
    const who = ROLE_LABEL[role] || "当前账号";
    return fail("公共文件区只对 VIP 及以上开放（" + who + "看不到，升级后自动可见）", 403);
  }

  // KV 的 list 一次最多 1000 个 key，这里翻页扫，但给自己留个上限：
  // 任务记录 7 天自动过期，一天最多 30 个，正常也就两三百条。
  const keys = [];
  let cursor;
  let scanTruncated = false;
  for (;;) {
    const page = await env.JOBS.list({ prefix: "job:", cursor, limit: 100 });
    for (const k of page.keys) keys.push(k.name);
    const done = page.list_complete || !page.cursor;
    if (done || keys.length >= COMMUNITY_SCAN) {
      if (!done) scanTruncated = true;
      break;
    }
    cursor = page.cursor;
  }

  const now = Date.now();
  const jobs = [];
  for (let i = 0; i < keys.length; i += 20) {
    // 一次并发读 20 条，别一条一条地等
    const batch = await Promise.all(keys.slice(i, i + 20).map((k) => env.JOBS.get(k, "json")));
    for (const job of batch) {
      if (!job || job.status !== "done" || !job.resultChunks) continue;
      if (isAutomationJob(job)) continue;                     // 自动化/自检的产物不进公共区
      const finishedAt = Number(job.doneAt || job.createdAt || 0);
      // 译文分块只留 3 天（BLOB_TTL），记录却留 7 天。过了 3 天的条目点下载只会拿到 410，
      // 与其让人白点一次，不如不进列表。
      if (!finishedAt || now - finishedAt > BLOB_TTL * 1000) continue;
      jobs.push({
        id: job.id,
        name: job.name || "未命名",
        modeLabel: MODE_LABEL[job.mode] || job.mode || "",
        target: job.target || "",
        pages: job.stats && job.stats.pages != null ? Number(job.stats.pages) || 0 : null,
        uploader: maskEmail(job.userEmail),
        uploaderFull: job.userEmail || "",                    // 只给管理员，见下面 map
        finishedAt,
        size: Number(job.size || job.resultSize || 0),
      });
    }
  }

  jobs.sort((a, b) => b.finishedAt - a.finishedAt);
  const isAdmin = role === "admin";
  const files = jobs.slice(0, COMMUNITY_LIMIT).map((j) => {
    const item = {
      id: j.id,
      name: j.name,
      modeLabel: j.modeLabel,
      target: j.target,
      pages: j.pages,
      uploader: j.uploader,
      finishedAt: j.finishedAt,
      size: j.size,
    };
    // 管理员看全称：出了事要能找到人，也方便对账。别人一律只看脱敏后的
    if (isAdmin) item.uploaderFull = j.uploaderFull;
    return item;
  });

  const truncated = jobs.length > COMMUNITY_LIMIT || scanTruncated;
  return json({
    ok: true,
    role,
    files,
    limit: COMMUNITY_LIMIT,
    scanned: keys.length,
    truncated,
    note: truncated ? "只列了最近 " + COMMUNITY_LIMIT + " 个，更早的没有显示" : "",
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === "/" || path === "/index.html") {
        return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8" } });
      }
      if (path === "/api/me") return await handleMe(request, env);
      if (path === "/api/verify") {
        const auth = await authorizeUser(request, env, url);
        return auth.error ? fail(auth.error, 401) : json({ ok: true, admin: !!auth.admin });
      }
      if (path === "/api/upload" && request.method === "PUT") return await handleUpload(request, env, ctx);
      if (path === "/api/status" && request.method === "GET") return await handleStatus(request, env, url);
      if (path === "/api/history" && request.method === "GET") return await handleHistory(request, env, url);
      if (path === "/api/community" && request.method === "GET") return await handleCommunity(request, env, url);
      if (path === "/api/download" && request.method === "GET") return await handleDownload(request, env, url);
      if (path.startsWith("/api/input/") && request.method === "GET") return await handleInput(request, env);
      if (path.startsWith("/api/result/") && request.method === "POST") return await handleResult(request, env);
      if (path.startsWith("/api/report/") && request.method === "POST") return await handleReport(request, env);
      return fail("没有这个接口：" + path, 404);
    } catch (err) {
      return fail("服务器内部错误：" + (err && err.message ? err.message : String(err)), 500);
    }
  },

  /**
   * 每天一次：走 KV 时什么都不用做（expirationTtl 会自动清）；
   * 以后要是绑了 R2，就用它删掉超过 7 天的对象。
   */
  async scheduled(event, env, ctx) {
    if (!env.FILES) return;
    const cutoff = Date.now() - 7 * 24 * 3600 * 1000;
    for (const prefix of ["inputs/", "results/"]) {
      let cursor;
      do {
        const page = await env.FILES.list({ prefix, cursor, limit: 200 });
        for (const obj of page.objects) {
          if (obj.uploaded && obj.uploaded.getTime() < cutoff) await env.FILES.delete(obj.key);
        }
        cursor = page.truncated ? page.cursor : undefined;
      } while (cursor);
    }
  },
};
