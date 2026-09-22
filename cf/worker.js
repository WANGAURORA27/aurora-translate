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

const MAX_UPLOAD = 95 * 1024 * 1024; // Workers 免费版请求体上限 100MB，留点余量
const CHUNK = 20 * 1024 * 1024; // KV 单值上限 25MiB，切 20MiB 一块最稳
const BLOB_TTL = 3 * 24 * 3600; // 原件与译文保留 3 天（KV 只有 1GB，靠 TTL 回收）
const JOB_TTL = 7 * 24 * 3600; // 任务记录保留 7 天，历史里还能看到
const DAILY_JOBS = 30; // 每天最多接这么多任务
const DAILY_BYTES = 250 * 1024 * 1024; // 每天最多收这么多字节（3 天滚动也在 1GB 之内）
const MODES = new Set(["inplace", "bilingual", "ocr"]);
const TARGETS = new Set(["中文", "英文", "日文", "韩文", "法文", "德文", "西班牙文", "俄文"]);

const MODE_LABEL = { inplace: "保持版式", bilingual: "中英对照", ocr: "扫描件 OCR" };

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

/** 问 GitHub 这个 run 到哪一步了，只在距离上次查超过 8 秒时才真去问（省调用次数） */
async function refreshFromGitHub(env, job) {
  if (!job.runId) return job;
  if (job.status === "done" || job.status === "failed") return job;
  if (Date.now() - (job.checkedAt || 0) < 8000) return job;

  job.checkedAt = Date.now();
  try {
    const resp = await ghFetch(env, "/actions/runs/" + job.runId);
    if (resp.ok) {
      const run = await resp.json();
      if (run.status === "completed") {
        if (run.conclusion === "success") {
          // 这一步的关键：KV 是**跨机房最终一致**的（最长约 60 秒），
          // Actions 把译文写进 KV 的那个机房，和这个用户请求落到的机房可能不是同一个。
          // 所以"运行成功但本机房还没看到译文"不能立刻判失败，否则会把好任务写成失败。
          // 给 2 分钟同步时间，超了才当真失败。
          if (job.status !== "done") {
            const finished = Date.parse(run.updated_at || "") || Date.now();
            if (Date.now() - finished > 120000) {
              job.status = "failed";
              job.note = "任务已结束，但没收到译文，请去 GitHub 看这次运行日志";
            } else {
              job.status = "running";
              job.note = "翻译已完成，译文正在同步，稍等一下…";
            }
          }
        } else if (run.conclusion === "cancelled") {
          job.status = "failed";
          job.note = "任务被取消";
        } else {
          job.status = "failed";
          job.note = "翻译失败（" + run.conclusion + "），常见原因：文件加密、语言不支持、额度用尽";
        }
      } else if (run.status === "in_progress") {
        job.status = "running";
      } else {
        job.status = "queued";
      }
    }

    // 再看一眼"具体跑到哪一步了"：把 Actions 里正在执行的那一步翻译成人话，
    // 顺便给出 第几步/共几步，页面上的进度条就能真的动起来。
    if (job.status === "running" || job.status === "queued") {
      const jobsResp = await ghFetch(env, "/actions/runs/" + job.runId + "/jobs");
      if (jobsResp.ok) {
        const data = await jobsResp.json();
        const list = data.jobs || [];
        const target = list.find((j) => j.name === "cloud") || list[0];
        if (target) {
          const steps = target.steps || [];
          const running = steps.findIndex((s) => s.status === "in_progress");
          const doneCount = steps.filter((s) => s.status === "completed").length;
          job.stepTotal = steps.length;
          job.stepIndex = running >= 0 ? running + 1 : Math.max(doneCount, 1);
          if (running >= 0) job.phase = phaseFor(steps[running].name);
          else if (target.status === "queued") job.phase = "已排队，等 GitHub 分配机器…";
        }
      }
    }
  } catch (err) {
    // 查进度失败不影响主流程，页面下次再问
  }
  return saveJob(env, job);
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

function authorizeUser(request, env, url) {
  const given = request.headers.get("x-password") || url.searchParams.get("password") || "";
  if (!env.PASSWORD) return "服务端还没设置口令（PASSWORD）";
  if (given !== env.PASSWORD) return "口令不对";
  return null;
}

function authorizeAgent(request, env) {
  if (!env.AGENT_KEY) return "服务端还没设置 AGENT_KEY";
  if (request.headers.get("x-agent-key") !== env.AGENT_KEY) return "内部密钥不对";
  return null;
}

// ── 接口 ─────────────────────────────────────────────────────────────

async function handleUpload(request, env, ctx) {
  const daren = authorizeUser(request, env, new URL(request.url));
  if (daren) return fail(daren, 401);

  const name = decodeURIComponent(request.headers.get("x-filename") || "");
  if (!name) return fail("没收到文件名");
  const lower = name.toLowerCase();
  if (!lower.endsWith(".pdf") && !lower.endsWith(".docx") && !lower.endsWith(".doc")) {
    return fail("只支持 PDF 和 Word（.pdf / .docx）");
  }

  const mode = request.headers.get("x-mode") || "inplace";
  if (!MODES.has(mode)) return fail("输出形式不对：" + mode);
  const target = request.headers.get("x-target") || "中文";
  if (!TARGETS.has(target)) return fail("目标语言不对：" + target);

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
    size,
    status: "queued",
    note: "已收到，正在排队",
    createdAt: Date.now(),
    backend: storeKind(env),
    inputChunks: written.count,
    inputSize: size,
    source: "cloud",
  };
  await saveJob(env, job);

  const resp = await ghFetch(
    env,
    "/actions/workflows/" + (env.WORKFLOW || "translate.yml") + "/dispatches",
    {
      method: "POST",
      body: JSON.stringify({ ref: env.GH_REF || "main", inputs: { job_id: id, mode, target } }),
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

async function handleStatus(request, env, url) {
  const daren = authorizeUser(request, env, url);
  if (daren) return fail(daren, 401);
  const id = url.searchParams.get("id");
  if (!id) return fail("缺少 id");
  let job = await readJob(env, id);
  if (!job) return fail("没有这个任务（记录保留 7 天）", 404);
  job = await refreshFromGitHub(env, job);
  const queued = job.status === "queued";
  const phase = job.status === "done"
    ? "翻译完成"
    : job.status === "failed"
      ? (job.note || "翻译失败")
      : job.phase || (queued ? "已排队，马上开始…" : "正在翻译…");
  return json({
    ok: true,
    id: job.id,
    name: job.name,
    mode: job.mode,
    modeLabel: MODE_LABEL[job.mode] || job.mode,
    target: job.target,
    size: job.size,
    status: job.status,
    // phase 是"给人看的当前阶段"，note 是更细的说明
    phase,
    note: job.note || "",
    stepIndex: job.stepIndex || 0,
    stepTotal: job.stepTotal || 0,
    // 从提交到现在过了多久（页面自己再往上加秒数，避免频繁请求）
    elapsedSec: Math.max(0, Math.round((Date.now() - (job.createdAt || Date.now())) / 1000)),
    runUrl: job.runUrl || "",
    ready: job.status === "done",
    stats: job.stats || null,
  });
}

async function handleDownload(request, env, url) {
  const daren = authorizeUser(request, env, url);
  if (daren) return fail(daren, 401);
  const id = url.searchParams.get("id");
  const job = await readJob(env, id);
  if (!job) return fail("没有这个任务", 404);
  if (!job.resultChunks) return fail("译文还没好", 409);

  const blob = await readBlob(env, id, "results", job.resultChunks);
  if (!blob) return fail("译文已过期（原件与译文只保留 3 天）", 410);
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

  const resultName = decodeURIComponent(request.headers.get("x-filename") || job.name);
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
    job.status = body.status;
  }
  if (body.note) job.note = String(body.note).slice(0, 500);
  if (body.stats) job.stats = body.stats;
  await saveJob(env, job);
  return json({ ok: true });
}

async function handleHistory(request, env, url) {
  const daren = authorizeUser(request, env, url);
  if (daren) return fail(daren, 401);
  const list = await env.JOBS.list({ prefix: "job:", limit: 100 });
  const jobs = [];
  for (const key of list.keys) {
    const job = await env.JOBS.get(key.name, "json");
    if (!job) continue;
    jobs.push({
      id: job.id,
      name: job.name,
      modeLabel: MODE_LABEL[job.mode] || job.mode,
      status: job.status,
      note: job.note || "",
      createdAt: job.createdAt,
      ready: job.status === "done",
      target: job.target,
    });
  }
  jobs.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  return json({ ok: true, jobs: jobs.slice(0, 30) });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === "/" || path === "/index.html") {
        return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8" } });
      }
      if (path === "/api/verify") {
        const daren = authorizeUser(request, env, url);
        return daren ? fail(daren, 401) : json({ ok: true });
      }
      if (path === "/api/upload" && request.method === "PUT") return await handleUpload(request, env, ctx);
      if (path === "/api/status" && request.method === "GET") return await handleStatus(request, env, url);
      if (path === "/api/history" && request.method === "GET") return await handleHistory(request, env, url);
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
