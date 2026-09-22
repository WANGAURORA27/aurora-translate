/* DocBridge · 前端逻辑（原生 JS，零依赖）
 *
 * 所有请求都用**相对 URL**（'api/jobs'），因此站点无论是挂在
 *   https://app.example.com/doc/   还是   https://translate.example.com/
 * 都不需要改一行代码。
 */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  cfg: null,
  file: null,
  format: null,        // 'pdf' | 'docx'
  mode: null,
  job: null,
  es: null,
  timer: null,
  pv: { in: 1, out: 1 },
  pvCount: { in: 1, out: 1 },
};

/* ---------------------------------------------------------------- 工具 */
function toast(msg, ms = 3200) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

const fmtSize = (n) => {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + u[i];
};

const fmtDur = (s) => {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' 秒';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + ' 分 ' + r + ' 秒';
  return Math.floor(m / 60) + ' 时 ' + (m % 60) + ' 分';
};

function detectFormat(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  const ext = m ? m[1].toLowerCase() : '';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'docx') return 'docx';
  return null;
}

/* ---------------------------------------------------------------- 初始化 */
async function init() {
  try {
    const r = await fetch('api/config');
    state.cfg = await r.json();
  } catch (e) {
    toast('无法连接服务端：' + e.message, 6000);
    return;
  }
  const c = state.cfg;

  // 语言
  const src = $('srcLang'), tgt = $('tgtLang');
  src.innerHTML = '<option value="auto">自动检测</option>' +
    c.languages.map((l) => `<option value="${l.code}">${l.name}</option>`).join('');
  tgt.innerHTML = c.languages.map((l) => `<option value="${l.code}">${l.name}</option>`).join('');
  src.value = c.defaults.source_lang;
  tgt.value = c.defaults.target_lang;

  // 预设代号：不可用的（密钥是占位符/含中文等）标 ⚠ 并禁选，
  // 否则用户选中后只会看到一句看不懂的底层报错
  const prof = $('profile');
  if (c.profiles.length) {
    prof.innerHTML = c.profiles.map((p) => {
      const warn = p.usable ? '' : ` ⚠ ${p.issue}`;
      return `<option value="${p.code}"${p.usable ? '' : ' disabled'}>` +
             `${p.code} · ${p.name}${p.model ? ' · ' + p.model : ''}${warn}</option>`;
    }).join('');
    const usable = c.profiles.filter((p) => p.usable);
    if (usable.length) prof.value = usable[0].code;
    prof.title = c.profiles.map((p) => `${p.code}: ${p.issue || p.note || p.name}`).join('\n');
    if (!usable.length) {
      const b = $('envBadge');
      b.hidden = false; b.className = 'badge bad';
      b.textContent = '没有可用的翻译通道';
      b.title = c.profiles.map((p) => `${p.code}: ${p.issue}`).join('\n');
    }
  } else {
    prof.innerHTML = '<option value="">（服务端未配置 profiles.json）</option>';
  }

  // 通道策略（兜底 / 混合路由 / 单一通道）
  const st = $('strategy');
  if (st) {
    st.innerHTML = (c.strategies || []).map((x) =>
      `<option value="${x.code}"${x.code === c.defaultStrategy ? ' selected' : ''}>${x.name}</option>`).join('');
    st.title = (c.strategies || []).map((x) => `${x.name}：${x.note}`).join('\n');
  }

  // 上限提示
  $('uploadHint').textContent =
    `支持 PDF 与 Word（.docx），单个文件最大 ${c.limits.maxUploadMB}MB。` +
    `文件只用于本次任务，约 ${c.limits.jobTTLHours} 小时后自动清理。`;

  if (c.adminEnabled) $('linkAdmin').hidden = false;

  // 同域下的 同类项目 入口（服务端用 DOCBRIDGE_LIVE_URL 配置，留空则不显示）
  const live = (c.links && c.links.live) || '';
  if (live) { $('linkLive').href = live; $('linkLive').hidden = false; }

  const errs = c.capabilities.pipelines.filter((p) => !p.available);
  if (errs.length) {
    const b = $('envBadge');
    b.hidden = false;
    b.className = 'badge bad';
    b.textContent = `${errs.length} 条管线不可用`;
    b.title = errs.map((e) => `${e.format}/${e.mode}: ${e.error}`).join('\n');
  }

  bindEvents();
  renderModes();
  loadHistory();
}

function bindEvents() {
  const drop = $('drop'), input = $('fileInput');
  drop.addEventListener('click', () => input.click());
  input.addEventListener('change', () => input.files[0] && pickFile(input.files[0]));

  ['dragenter', 'dragover'].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) pickFile(f);
  });

  $('btnClearFile').addEventListener('click', () => { state.file = null; renderFile(); });
  $('btnStart').addEventListener('click', startJob);
  $('btnCancel').addEventListener('click', cancelJob);
  $('btnTest').addEventListener('click', testChannel);
  $('tgtLang').addEventListener('change', () => { if (state.job) return; });
  document.querySelectorAll('[data-pv]').forEach((b) =>
    b.addEventListener('click', () => stepPreview(b.dataset.pv, Number(b.dataset.d))));
}

/* ---------------------------------------------------------------- 选文件 */
function pickFile(f) {
  const fmt = detectFormat(f.name);
  if (!fmt) { toast('只支持 .pdf 与 .docx 文件'); return; }
  if (f.size > state.cfg.limits.maxUploadMB * 1024 * 1024) {
    toast(`文件 ${fmtSize(f.size)} 超过上限 ${state.cfg.limits.maxUploadMB}MB`);
    return;
  }
  state.file = f;
  state.format = fmt;
  renderFile();
  renderModes();
}

function renderFile() {
  const f = state.file;
  $('drop').hidden = !!f;
  $('fileInfo').hidden = !f;
  if (f) {
    $('fileName').textContent = f.name;
    $('fileSize').textContent = fmtSize(f.size);
    $('fileKind').textContent = state.format === 'pdf' ? 'PDF' : 'Word (.docx)';
  }
  refreshStart();
}

/* ---------------------------------------------------------------- 模式 */
function pipelinesFor(fmt) {
  return (state.cfg.capabilities.pipelines || []).filter((p) => p.format === fmt);
}

function renderModes() {
  const box = $('modes');
  if (!state.format) {
    box.innerHTML = '<div class="hint" style="margin:0">先选择文件，这里会列出可用的输出形式。</div>';
    $('opts').innerHTML = '';
    refreshStart();
    return;
  }
  const list = pipelinesFor(state.format);
  box.innerHTML = list.map((p) => {
    const dis = p.available ? '' : 'disabled';
    const note = p.available ? (p.note || '') : `不可用：${p.error || '加载失败'}`;
    return `<button class="mode${state.mode === p.mode ? ' on' : ''}" data-mode="${p.mode}" ${dis}>
        <div class="t">${p.label}</div><div class="n">${note}</div>
      </button>`;
  }).join('');

  box.querySelectorAll('.mode').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.mode = btn.dataset.mode;
      renderModes();
      renderOptions();
    });
  });

  const avail = list.filter((p) => p.available);
  if (!avail.some((p) => p.mode === state.mode)) state.mode = avail.length ? avail[0].mode : null;
  box.querySelectorAll('.mode').forEach((btn) =>
    btn.classList.toggle('on', btn.dataset.mode === state.mode));
  renderOptions();
  refreshStart();
}

function currentPipeline() {
  return pipelinesFor(state.format).find((p) => p.mode === state.mode) || null;
}

/* 参数控件由管线声明的 OPTIONS 自动生成：加参数不用改前端 */
function renderOptions() {
  const p = currentPipeline();
  const box = $('opts');
  if (!p || !p.options || !p.options.length) { box.innerHTML = ''; return; }
  box.innerHTML = p.options.map((o) => {
    const id = 'opt_' + o.key;
    if (o.type === 'bool') {
      return `<div class="opt"><label class="chk" style="margin:0">
        <input type="checkbox" id="${id}" data-opt="${o.key}" ${o.default ? 'checked' : ''} />
        <span>${o.label}</span></label>
        <div class="hint" style="margin:4px 0 0">${o.hint || ''}</div></div>`;
    }
    if (o.type === 'number') {
      return `<div class="opt"><div class="t">${o.label}</div>
        <div class="range"><input type="range" id="${id}" data-opt="${o.key}"
          min="${o.min ?? 0}" max="${o.max ?? 2}" step="${o.step ?? 0.05}" value="${o.default ?? 1}" />
          <span class="val" id="${id}_v">${o.default ?? 1}</span></div>
        <div class="hint" style="margin:4px 0 0">${o.hint || ''}</div></div>`;
    }
    if (o.type === 'select') {
      return `<div class="opt"><label>${o.label}
        <select id="${id}" data-opt="${o.key}">${(o.choices || []).map(([v, t]) =>
          `<option value="${v}"${v === o.default ? ' selected' : ''}>${t}</option>`).join('')}</select></label>
        <div class="hint" style="margin:4px 0 0">${o.hint || ''}</div></div>`;
    }
    return `<div class="opt"><label>${o.label}
      <input id="${id}" data-opt="${o.key}" value="${o.default ?? ''}" /></label>
      <div class="hint" style="margin:4px 0 0">${o.hint || ''}</div></div>`;
  }).join('');

  box.querySelectorAll('[type=range]').forEach((r) =>
    r.addEventListener('input', () => { $(r.id + '_v').textContent = r.value; }));
}

function collectOptions() {
  const out = {};
  document.querySelectorAll('[data-opt]').forEach((el) => {
    const k = el.dataset.opt;
    if (el.type === 'checkbox') out[k] = el.checked;
    else if (el.type === 'range' || el.type === 'number') out[k] = Number(el.value);
    else if (el.value !== '') out[k] = el.value;
  });
  return out;
}

function refreshStart() {
  const ok = !!(state.file && state.mode && currentPipeline() && currentPipeline().available);
  $('btnStart').disabled = !ok;
}

/* ---------------------------------------------------------------- 提交任务 */
async function startJob() {
  if (!state.file || !state.mode) return;
  const fd = new FormData();
  fd.append('file', state.file);
  fd.append('mode', state.mode);
  fd.append('profile', $('profile').value || '');
  fd.append('strategy', ($('strategy') && $('strategy').value) || 'fallback');
  fd.append('source_lang', $('srcLang').value);
  fd.append('target_lang', $('tgtLang').value);
  fd.append('options', JSON.stringify(collectOptions()));
  const b = $('apiBaseUrl').value.trim(), k = $('apiKey').value.trim(), m = $('apiModel').value.trim();
  if (b) fd.append('apiBaseUrl', b);
  if (k) fd.append('apiKey', k);
  if (m) fd.append('apiModel', m);

  $('btnStart').disabled = true;
  $('btnStart').textContent = '上传中…';
  let job;
  try {
    const r = await fetch('api/jobs', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    job = data;
  } catch (e) {
    toast('提交失败：' + e.message, 6000);
    $('btnStart').textContent = '开始翻译';
    refreshStart();
    return;
  }
  $('btnStart').textContent = '开始翻译';
  $('cardResult').hidden = true;
  $('cardProgress').hidden = false;
  $('jobLogs').textContent = '';
  loggedSet = new Set();          // 新任务：清掉上一个任务的去重记录
  state.job = job;
  $('jobName').textContent = `${job.srcName}（${fmtSize(job.srcSize)}）`;
  setPill(job.status);
  setBar(0, 0);
  subscribe(job.id);
  loadHistory();
}

function setPill(status) {
  const map = { queued: '排队中', running: '翻译中', done: '已完成', error: '失败', canceled: '已取消' };
  const el = $('jobPill');
  el.className = 'pill ' + status;
  el.textContent = map[status] || status;
}

function setBar(done, total) {
  const bar = $('jobBar');
  if (!total) { bar.classList.add('indet'); bar.querySelector('i').style.width = ''; return; }
  bar.classList.remove('indet');
  bar.querySelector('i').style.width = Math.min(100, Math.round((done / total) * 100)) + '%';
}

function subscribe(id) {
  if (state.es) state.es.close();
  startTimer();
  const es = new EventSource(`api/jobs/${id}/events`);
  state.es = es;

  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === 'progress') {
      setPill('running');
      setBar(d.done, d.total);
      $('jobNote').textContent = d.note || `${d.done}/${d.total}`;
    } else if (d.type === 'log') {
      // 走去重版本：这些行在任务结束时还会从 /api/jobs/<id> 的 logs 再拉一次，
      // 不去重就会看到「开始处理…」「翻译 N 条」重复两遍
      appendLogOnce(d.message);
    } else if (d.type === 'status') {
      setPill(d.status);
      if (d.progress) setBar(d.progress.done, d.progress.total);
      if (d.status === 'done' || d.status === 'error' || d.status === 'canceled') {
        es.close();
        state.es = null;
        stopTimer();
        finish(id);
      }
    }
  };
  es.onerror = () => {
    // 连接断了不一定代表任务失败：退化为轮询一次实际状态
    es.close();
    state.es = null;
    stopTimer();
    finish(id);
  };
}

function appendLog(line) {
  const box = $('jobLogs');
  const span = document.createElement('div');
  span.textContent = line;
  if (/^\[warn\]/.test(line)) span.className = 'w';
  if (/^\[error\]/.test(line)) span.className = 'e';
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

function startTimer() {
  stopTimer();
  const t0 = Date.now();
  state.timer = setInterval(() => {
    $('jobElapsed').textContent = '已用 ' + fmtDur((Date.now() - t0) / 1000);
  }, 1000);
}
function stopTimer() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

async function finish(id) {
  let job;
  try {
    const r = await fetch(`api/jobs/${id}`);
    job = await r.json();
  } catch { return; }
  if (!job || !job.status) return;
  state.job = job;
  setPill(job.status);
  if (job.progress) setBar(job.progress.done, job.progress.total);
  $('jobNote').textContent = job.progress && job.progress.note ? job.progress.note : job.status;
  (job.logs || []).forEach((l) => { if (!/^\s*$/.test(l)) appendLogOnce(l); });

  if (job.status === 'error') {
    toast('任务失败：' + (job.error || '未知原因'), 8000);
    $('btnCancel').hidden = true;
  } else if (job.status === 'canceled') {
    $('btnCancel').hidden = true;
  } else if (job.status === 'done') {
    $('btnCancel').hidden = true;
    showResult(job);
  }
  loadHistory();
}

let loggedSet = new Set();
function appendLogOnce(line) {
  if (loggedSet.has(line)) return;
  loggedSet.add(line);
  appendLog(line);
}

function showResult(job) {
  if (!job.result) return;
  $('cardResult').hidden = false;
  $('resName').textContent = job.result.name;
  $('resSize').textContent = fmtSize(job.result.size);
  $('resDetail').textContent = (job.stats && job.stats.detail) || '';
  $('btnDownload').href = `api/jobs/${job.id}/download`;
  $('btnSource').href = `api/jobs/${job.id}/source`;

  const s = job.stats || {};
  const rows = [
    ['翻译单元', s.units], ['页数', s.pages], ['跳过', s.skipped],
    ['送入字符', s.chars_in], ['译文字符', s.chars_out], ['接口调用', s.api_calls],
  ].filter(([, v]) => v !== undefined && v !== null && v !== '');
  const failed = s.api_failed || 0;
  $('resStats').innerHTML = rows.map(([k, v]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('') +
    (failed ? `<div class="stat"><div class="k">未译处</div><div class="v err">${failed}</div></div>` : '');

  const isPdf = /\.pdf$/i.test(job.result.name);
  $('previews').hidden = !isPdf;
  $('noPreview').hidden = isPdf;
  if (!isPdf) {
    $('noPreview').textContent = 'Word 文件暂不支持在线预览，下载后用 Word / Pages / WPS 打开即可。';
  } else {
    state.pv = { in: 1, out: 1 };
    loadPreview('in');
    loadPreview('out');
  }
  $('cardResult').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function loadPreview(which) {
  const job = state.job;
  if (!job) return;
  const page = state.pv[which];
  const img = which === 'in' ? $('imgIn') : $('imgOut');
  try {
    const r = await fetch(`api/jobs/${job.id}/preview?which=${which}&page=${page}`);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      img.replaceWith(Object.assign(document.createElement('div'),
        { className: 'nopv', textContent: e.error || '无法预览' }));
      return;
    }
    state.pvCount[which] = Number(r.headers.get('X-Page-Count') || 1);
    const blob = await r.blob();
    if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(blob);
    $(which === 'in' ? 'pgIn' : 'pgOut').textContent = `${page} / ${state.pvCount[which]}`;
  } catch (e) {
    toast('预览失败：' + e.message);
  }
}

function stepPreview(which, d) {
  const next = Math.min(Math.max(1, state.pv[which] + d), state.pvCount[which] || 1);
  if (next === state.pv[which]) return;
  state.pv[which] = next;
  loadPreview(which);
}

async function cancelJob() {
  if (!state.job) return;
  await fetch(`api/jobs/${state.job.id}/cancel`, { method: 'POST' });
  toast('已请求取消');
}

async function testChannel() {
  const btn = $('btnTest');
  btn.disabled = true;
  btn.textContent = '测试中…';
  try {
    const body = {
      profile: $('profile').value || '',
      target_lang: $('tgtLang').value,
      apiBaseUrl: $('apiBaseUrl').value.trim(),
      apiKey: $('apiKey').value.trim(),
      apiModel: $('apiModel').value.trim(),
    };
    const r = await fetch('api/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) toast(`通道正常（${d.ms}ms，${d.model || '默认模型'}）：${d.sample}`, 6000);
    else toast('通道不可用：' + (d.error || '未知原因'), 8000);
  } catch (e) {
    toast('测试失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '测试通道';
  }
}

/* ---------------------------------------------------------------- 历史 */
async function loadHistory() {
  let jobs = [];
  try {
    const r = await fetch('api/jobs');
    jobs = (await r.json()).jobs || [];
  } catch { return; }
  const box = $('hist');
  if (!jobs.length) { $('cardHistory').hidden = true; return; }
  $('cardHistory').hidden = false;
  const label = { queued: '排队中', running: '翻译中', done: '已完成', error: '失败', canceled: '已取消' };
  box.innerHTML = jobs.map((j) => {
    const dl = j.status === 'done' && j.result
      ? `<a class="ghostlink" href="api/jobs/${j.id}/download">下载</a>` : '';
    const when = new Date(j.created * 1000).toLocaleString('zh-CN', { hour12: false });
    return `<div class="hitem">
      <div class="nm">${j.srcName}<div class="hint" style="margin:2px 0 0">${when} ·
        ${j.format}/${j.mode} · ${fmtSize(j.srcSize)}</div></div>
      <div style="display:flex; gap:10px; align-items:center">
        <span class="pill ${j.status}">${label[j.status] || j.status}</span>${dl}
      </div></div>`;
  }).join('');
}

init();
