/**
 * aurora-translate 的云端中转层（Cloudflare Worker）
 *
 * 它解决三件事：
 *   1. 密码门     —— 只有拿到口令的人能用，翻译的钱（你的 API key）不会被陌生人消耗
 *   2. 大文件     —— GitHub Issue 附件上限 25MB，这里用 R2 存原件和译文，能到 95MB
 *   3. 进度与下载 —— 页面实时看进度，译完直接点下载，不用去 GitHub 翻
 *
 * 文件流：
 *   浏览器 --PUT /api/upload--> R2(inputs/) --dispatch--> GitHub Actions
 *   Actions --GET /api/input/<id>--> 取原件
 *   Actions --POST /api/result/<id>--> 回传译文到 R2(results/)
 *   Actions --POST /api/status/<id>--> 回传进度/统计
 *   浏览器 --GET /api/status?id=--> 看进度；--GET /api/download?id=--> 下载
 */

import { PAGE } from "./page.js";

const MAX_UPLOAD = 95 * 1024 * 1024; // Workers 免费版请求体上限 100MB，留点余量
const JOB_TTL = 7 * 24 * 3600; // KV 记录保留 7 天，和 results 分支清理一致
const JOB_RETENTION_MS = 7 * 24 * 3600 * 1000;
const MODES = new Set(["inplace", "bilingual", "ocr"]);
const TARGETS = new Set(["中文", "英文", "日文", "韩文", "法文", "德文", "西班牙文", "俄文"]);

const MODE_LABEL = { inplace: "保持版式", bilingual: "中英对照", ocr: "扫描件 OCR" };

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
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

async function readJob(env, id) {
  return env.JOBS.get(jobKey(id), "json");
}

async function saveJob(env, job) {
  await env.JOBS.put(jobKey(job.id), JSON.stringify(job), { expirationTtl: JOB_TTL });
  return job;
}

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
          // 成功但没收到译文回传，多半是回传那一步挂了
          if (job.status !== "done") {
            job.status = "failed";
            job.note = "任务已结束，但没收到译文，请去 GitHub 看这次运行日志";
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
  } catch (err) {
    // 查进度失败不影响主流程，页面下次再问
  }
  return saveJob(env, job);
}

/** workflow_dispatch 不返回 run id，用 run-name 里带的 job_id 去列表里认领 */
async function adoptRun(env, jobId) {
  for (let attempt = 0; attempt < 6; attempt++) {
    await new Promise((r) => setTimeout(r, 1500));
    const resp = await ghFetch(env, "/actions/workflows/" + (env.WORKFLOW || "translate.yml") + "/runs?event=workflow_dispatch&per_page=10");
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

  const id = crypto.randomUUID().replace(/-/g, "").slice(0, 20);
  const key = "inputs/" + id + "/" + name;
  await env.FILES.put(key, request.body, { httpMetadata: { contentType: "application/octet-stream" } });

  const job = {
    id,
    name,
    mode,
    target,
    size: declared,
    status: "queued",
    note: "已收到，正在排队",
    createdAt: Date.now(),
    inputKey: key,
    source: "cloud",
  };
  await saveJob(env, job);

  const resp = await ghFetch(env, "/actions/workflows/" + (env.WORKFLOW || "translate.yml") + "/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: env.GH_REF || "main", inputs: { job_id: id, mode, target } }),
  });
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
  return json({
    ok: true,
    id: job.id,
    name: job.name,
    mode: job.mode,
    modeLabel: MODE_LABEL[job.mode] || job.mode,
    target: job.target,
    size: job.size,
    status: job.status,
    note: job.note || "",
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
  if (!job.resultKey) return fail("译文还没好", 409);

  const head = await env.FILES.head(job.resultKey);
  if (!head) return fail("译文已过期（保留 7 天）", 410);
  const obj = await env.FILES.get(job.resultKey);
  const filename = job.resultName || "translated.pdf";
  return new Response(obj.body, {
    headers: {
      "content-type": "application/octet-stream",
      "content-length": String(head.size),
      "content-disposition":
        "attachment; filename=\"translated" + (filename.match(/\.[a-z0-9]+$/i) || [".pdf"])[0] + "\"; filename*=UTF-8''" + encodeURIComponent(filename),
      "cache-control": "no-store",
    },
  });
}

async function handleInput(request, env) {
  const bad = authorizeAgent(request, env);
  if (bad) return fail(bad, 401);
  const id = new URL(request.url).pathname.split("/").pop();
  const job = await readJob(env, id);
  if (!job) return fail("没有这个任务", 404);
  const obj = await env.FILES.get(job.inputKey);
  if (!obj) return fail("原件不见了", 404);
  return new Response(obj.body, {
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

  const resultName = decodeURIComponent(request.headers.get("x-filename") || job.name);
  const key = "results/" + id + "/" + resultName;
  await env.FILES.put(key, request.body, { httpMetadata: { contentType: "application/octet-stream" } });

  job.resultKey = key;
  job.resultName = resultName;
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
  if (body.status && ["queued", "running", "done", "failed"].includes(body.status)) job.status = body.status;
  if (body.note) job.note = String(body.note).slice(0, 500);
  if (body.stats) job.stats = body.stats;
  await saveJob(env, job);
  return json({ ok: true });
}

async function handleHistory(request, env, url) {
  const daren = authorizeUser(request, env, url);
  if (daren) return fail(daren, 401);
  const list = await env.JOBS.list({ prefix: "job:", limit: 60 });
  const jobs = [];
  for (const key of list.keys) {
    const job = await env.JOBS.get(key.name, "json");
    if (!job) continue;
    jobs.push({
      id: job.id, name: job.name, modeLabel: MODE_LABEL[job.mode] || job.mode,
      status: job.status, note: job.note || "", createdAt: job.createdAt,
      ready: job.status === "done", target: job.target,
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

  /** 每天清一次 R2 里超过 7 天的原件和译文，别让存储无限长大 */
  async scheduled(event, env, ctx) {
    const cutoff = Date.now() - JOB_RETENTION_MS;
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
