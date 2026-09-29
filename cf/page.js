/**
 * 页面本身（单文件，无外部依赖，Worker 直接把它吐给浏览器）
 * 注意：这里是模板字符串，里面不要出现反引号和 ${，内联脚本统一用单引号拼接。
 */

export const PAGE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aurora 文档翻译</title>
<style>
  :root { --ink:#16181d; --sub:#6b7280; --line:#e5e7eb; --brand:#2f6df6; --ok:#0f9d58; --bad:#d93025; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
         color:var(--ink); background:#f6f7f9; }
  .wrap { max-width:760px; margin:0 auto; padding:32px 20px 64px; }
  h1 { font-size:22px; margin:0 0 6px; }
  .lead { color:var(--sub); margin:0 0 24px; font-size:14px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 14px; }
  label { display:block; font-size:13px; color:var(--sub); margin:12px 0 6px; }
  input[type=password], input[type=text], select { width:100%; padding:10px 12px; border:1px solid var(--line);
         border-radius:8px; font-size:14px; background:#fff; }
  input[type=file] { width:100%; font-size:14px; }
  button { background:var(--brand); color:#fff; border:0; border-radius:8px; padding:11px 18px;
           font-size:15px; cursor:pointer; }
  button:disabled { background:#b9c3d6; cursor:not-allowed; }
  .row { display:flex; gap:12px; }
  .row > div { flex:1; }
  /* 「精修」勾选框：不加解释文字，只有两个字 */
  label.chk { display:flex; align-items:center; gap:8px; font-size:14px; color:var(--ink);
              margin:14px 0 0; cursor:pointer; }
  label.chk input { width:auto; margin:0; }
  .hidden { display:none !important; }
  .bar { height:8px; background:#eef1f6; border-radius:99px; overflow:hidden; margin:14px 0 8px; }
  .bar > i { display:block; height:100%; width:0; background:var(--brand); transition:width .25s; }
  .spin { display:inline-block; width:12px; height:12px; margin-right:8px; border:2px solid #cfd8e8;
          border-top-color:var(--brand); border-radius:50%; animation:aurora-spin .9s linear infinite;
          vertical-align:-2px; }
  @keyframes aurora-spin { to { transform: rotate(360deg); } }
  .muted { color:var(--sub); font-size:13px; }
  .status { font-weight:600; }
  .status.done { color:var(--ok); }
  .status.failed { color:var(--bad); }
  .dl { display:inline-block; margin-top:12px; background:var(--ok); color:#fff; text-decoration:none;
        padding:10px 16px; border-radius:8px; }
  ul.hist { list-style:none; margin:0; padding:0; }
  ul.hist li { border-top:1px solid var(--line); padding:10px 0; font-size:14px; display:flex;
               justify-content:space-between; gap:12px; align-items:center; }
  ul.hist li:first-child { border-top:0; }
  .tag { font-size:12px; color:var(--sub); }
  /* 公共文件区：表格窄屏放不下就横向滚，别把卡片撑破 */
  .tablewrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table.comm { width:100%; border-collapse:collapse; font-size:14px; min-width:620px; }
  table.comm th { text-align:left; font-weight:500; color:var(--sub); font-size:12px;
                  padding:0 12px 8px 0; border-bottom:1px solid var(--line); white-space:nowrap; }
  table.comm td { padding:10px 12px 10px 0; border-top:1px solid var(--line); vertical-align:top; }
  table.comm tr:first-child td { border-top:0; }
  table.comm td.cname { max-width:230px; word-break:break-all; }
  table.comm th:last-child, table.comm td:last-child { padding-right:0; }
  table.comm a.cdl { color:var(--ok); text-decoration:none; white-space:nowrap; }
  code { background:#f1f3f7; padding:2px 6px; border-radius:5px; font-size:13px; }
  .tip { font-size:13px; color:var(--sub); margin-top:10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Aurora 文档翻译</h1>
  <p class="lead">上传 PDF / Word，保持原来的排版把文字换成中文，公式和图表都会留着。</p>

  <div class="card" id="authcard">
    <h2>账号</h2>
    <p class="status" id="authstate">正在检查登录状态…</p>
    <p class="muted" id="authquota"></p>
    <div id="authlogin" class="hidden" style="margin-top:14px">
      <a class="dl" href="https://account.ourmetaverse.cn/" target="_blank" rel="noopener">去登录 / 注册</a>
      <p class="tip">登录后回到本页即可开始翻译。没有账号？注册只需一个邮箱收验证码。</p>
    </div>
    <details id="pwbox" style="margin-top:16px">
      <summary class="muted" style="cursor:pointer">管理员 / 自动化：改用管理口令</summary>
      <input type="password" id="pw" placeholder="管理口令" autocomplete="current-password" style="margin-top:10px">
      <p class="tip" id="gatem">管理口令是应急通道，不占任何人的额度。</p>
      <div style="margin-top:10px"><button id="gobtn">用口令进入</button></div>
    </details>
  </div>

  <div id="app" class="hidden">
    <div class="card">
      <h2>上传文件</h2>
      <input type="file" id="file" accept=".pdf,.docx,.doc">
      <div class="row">
        <div>
          <label>输出形式</label>
          <select id="mode">
            <option value="inplace">保持版式（中文替换原文）</option>
            <option value="bilingual">中英对照（双栏逐段）</option>
            <option value="ocr">扫描件 OCR（图片型 PDF）</option>
          </select>
        </div>
        <div>
          <label>目标语言</label>
          <select id="target">
            <option>中文</option><option>英文</option><option>日文</option>
            <option>韩文</option><option>法文</option><option>德文</option>
            <option>西班牙文</option><option>俄文</option>
          </select>
        </div>
      </div>
      <!-- 精修：默认不勾；只有 can_refine 的账号才显示（见 checkAuth） -->
      <label class="chk hidden" id="refinebox"><input type="checkbox" id="refine"><span>精修</span></label>
      <div style="margin-top:16px"><button id="upbtn">开始翻译</button></div>
      <div class="bar hidden" id="ubar"><i></i></div>
      <p class="tip" id="upnote">单个文件最大 95MB。30 页大约 1 分钟，几百页的教材会久一些。译文保留 3 天，请及时下载。</p>
    </div>

    <div class="card hidden" id="jobcard">
      <h2>翻译进度</h2>
      <p class="status" id="jstatus"><span class="spin" id="jspin"></span><span id="jtext">排队中</span></p>
      <p class="muted" id="jname"></p>
      <div class="bar"><i id="jbar"></i></div>
      <p class="muted" id="jstep"></p>
      <p class="muted" id="jnote"></p>
      <p class="muted" id="jstats"></p>
      <a class="dl hidden" id="jdl" href="#">下载译文</a>
      <p class="tip hidden" id="jlink"></p>
    </div>

    <div class="card">
      <h2>最近的任务</h2>
      <ul class="hist" id="hist"><li class="muted">暂无记录</li></ul>
    </div>

    <!-- 公共文件区：只有 VIP / 管理员可见（见 checkAuth；服务端 /api/community 还会再判一次角色） -->
    <div class="card hidden" id="commcard">
      <h2>公共文件</h2>
      <p class="muted" id="commnote">别人翻译好的文件，VIP 及以上可以直接下载。只显示 3 天内的译文。</p>
      <div class="tablewrap">
        <table class="comm">
          <thead>
            <tr>
              <th>文件名</th><th>输出形式</th><th>页数</th><th>上传者</th><th>完成时间</th><th></th>
            </tr>
          </thead>
          <tbody id="commrows"></tbody>
        </table>
      </div>
      <p class="muted hidden" id="commempty">还没有别人翻译过的文件</p>
    </div>
  </div>
</div>

<script>
var pw = sessionStorage.getItem('aurora_pw') || '';
var authed = false;          // 已通过登录或管理口令验证
var canRefine = false;       // 当前账号有没有「精修」能力（由 /api/me 决定）
var role = '';               // 当前账号角色：user / vip / admin（公共文件区是否可见看它）
// 公共区只列 3 天内的译文（译文存储只留 3 天，更早的点下载只会 410），说明文案跟 HTML 里那句保持一致
var COMM_NOTE = '别人翻译好的文件，VIP 及以上可以直接下载。只显示 3 天内的译文。';
var polling = null;
var elapsedBase = 0;     // 服务端给的已用秒数
var lastJob = null;      // 最近一次状态，供本地秒表使用
var clockTimer = null;

function $(id) { return document.getElementById(id); }

/** 本地秒表：每秒钟把"已用时间"往上加，不用一直去问服务器 */
function startClock() {
  if (clockTimer) return;
  clockTimer = setInterval(function () {
    if (!lastJob) return;
    if (lastJob.status !== 'running' && lastJob.status !== 'queued') return;
    elapsedBase += 1;
    tickClock(lastJob);
  }, 1000);
}

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ 'x-password': pw }, opts.headers || {});
  return fetch(path, opts);
}

/** 显示可用的上传界面 */
function showApp() {
  $('authcard').classList.add('hidden');
  $('app').classList.remove('hidden');
  refreshLists();
}

/** 用登录状态进入（阶段 2 的正常路径） */
function checkAuth() {
  api('/api/me').then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok && d.logged_in) {
      authed = true;
      role = d.user.role || 'user';
      var tag = role === 'admin' ? '（管理员）' : (role === 'vip' ? '（VIP）' : '');
      $('authstate').textContent = '已登录：' + d.user.email + tag;
      $('authquota').textContent = d.quota.unlimited
        ? '额度：不限'
        : '本月剩余 ' + d.quota.remaining + ' 页（已用 ' + d.quota.used + ' / ' + d.quota.quota + ' 页）';
      // 精修只有账号带能力位时才给看；服务端还会再校验一遍（前端藏起来不是权限）
      canRefine = d.user.can_refine === 1;
      if (canRefine) $('refinebox').classList.remove('hidden');
      // 公共文件区：VIP 起可见。藏起来只是不碍眼，真正的门在服务端
      if (role === 'vip' || role === 'admin') $('commcard').classList.remove('hidden');
      showApp();
      return;
    }
    authed = false;
    canRefine = false;
    role = '';
    $('refinebox').classList.add('hidden');
    $('commcard').classList.add('hidden');
    $('authstate').textContent = '还没有登录';
    $('authquota').textContent = '翻译需要先登录 —— 每个人的额度单独计算，互不影响。';
    $('authlogin').classList.remove('hidden');
  }).catch(function () {
    $('authstate').textContent = '连不上服务器，稍后再试';
  });
}

/** 管理口令通道（自动化自检 / 应急） */
$('gobtn').onclick = function () {
  pw = $('pw').value.trim();
  if (!pw) { $('gatem').textContent = '先填口令'; return; }
  fetch('/api/verify', { headers: { 'x-password': pw } }).then(function (r) {
    if (!r.ok) { $('gatem').textContent = '口令不对，再试一次'; return; }
    sessionStorage.setItem('aurora_pw', pw);
    authed = true;
    showApp();
  }).catch(function () { $('gatem').textContent = '连不上服务器，稍后再试'; });
};
$('pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('gobtn').click(); });

$('upbtn').onclick = function () {
  if (!authed) { $('upnote').textContent = '请先登录（或输入管理口令）'; return; }
  var f = $('file').files[0];
  if (!f) { $('upnote').textContent = '先选一个文件'; return; }
  var bar = $('ubar'), fill = bar.querySelector('i');
  bar.classList.remove('hidden');
  $('upbtn').disabled = true;
  $('upnote').textContent = '正在上传…';

  var xhr = new XMLHttpRequest();
  xhr.open('PUT', '/api/upload');
  xhr.setRequestHeader('x-password', pw);
  xhr.setRequestHeader('x-filename', encodeURIComponent(f.name));
  xhr.setRequestHeader('x-mode', $('mode').value);
  xhr.setRequestHeader('x-target', encodeURIComponent($('target').value));
  // 明确送 0/1：服务端据 can_refine 复核，前端传来的 1 不算数
  xhr.setRequestHeader('x-refine', (canRefine && $('refine').checked) ? '1' : '0');
  xhr.upload.onprogress = function (e) {
    if (e.lengthComputable) {
      fill.style.width = (e.loaded / e.total * 100).toFixed(0) + '%';
      $('upnote').textContent = '正在上传 ' + (e.loaded / 1048576).toFixed(1) + ' / ' + (e.total / 1048576).toFixed(1) + ' MB';
    }
  };
  xhr.onload = function () {
    $('upbtn').disabled = false;
    var data = {};
    try { data = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status !== 200 || !data.ok) {
      $('upnote').textContent = data.error || ('上传失败（' + xhr.status + '）');
      bar.classList.add('hidden');
      return;
    }
    $('upnote').textContent = '上传完成，已交给后台翻译。';
    fill.style.width = '100%';
    $('file').value = '';
    watch(data.id, data.name, data.mode, data.target);
  };
  xhr.onerror = function () {
    $('upbtn').disabled = false;
    $('upnote').textContent = '网络中断，上传失败';
    bar.classList.add('hidden');
  };
  xhr.send(f);
};

function watch(id, name, mode, target) {
  $('jobcard').classList.remove('hidden');
  $('jname').textContent = name + ' · ' + (mode === 'bilingual' ? '中英对照' : mode === 'ocr' ? '扫描件 OCR' : '保持版式') + ' → ' + target;
  $('jdl').classList.add('hidden');
  $('jlink').classList.add('hidden');
  $('jdl').href = '/api/download?id=' + id + '&password=' + encodeURIComponent(pw);
  $('jdl').setAttribute('download', '');
  if (polling) clearInterval(polling);
  poll(id);
  polling = setInterval(function () { poll(id); }, 4000);
  startClock();
  $('jobcard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function poll(id) {
  api('/api/status?id=' + id).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { setJobText(d.error || '查询失败', 'failed'); return; }
    lastJob = d;
    var running = d.status === 'running' || d.status === 'queued';
    setJobText(d.phase || (d.status === 'done' ? '翻译完成' : d.status === 'failed' ? '翻译失败' : '处理中…'),
               d.status === 'done' ? 'done' : d.status === 'failed' ? 'failed' : '', running);

    // 进度条：优先用"第几步/共几步"这个真实比例，没有就退回粗估
    var pct = 12;
    if (d.status === 'done') pct = 100;
    else if (d.stepTotal > 0 && d.stepIndex > 0) pct = Math.min(96, Math.round(d.stepIndex / d.stepTotal * 100));
    else if (d.status === 'running') pct = 55;
    $('jbar').style.width = pct + '%';

    elapsedBase = d.elapsedSec || 0;
    tickClock(d);
    $('jnote').textContent = d.note || '';
    if (d.stats) {
      var s = d.stats;
      var bits = [];
      if (s.pages) bits.push(s.pages + ' 页');
      if (s.units) bits.push(s.units + ' 段');
      if (s.api_calls) bits.push('调用 ' + s.api_calls + ' 次');
      if (s.api_tokens_in) bits.push('入 ' + s.api_tokens_in + ' tokens');
      if (s.seconds) bits.push('耗时 ' + Math.round(s.seconds) + ' 秒');
      if (s.outputMB) bits.push(s.outputMB + ' MB');
      $('jstats').textContent = bits.join(' · ');
    }
    if (d.ready) {
      $('jdl').classList.remove('hidden');
      if (polling) { clearInterval(polling); polling = null; }
      refreshLists();
    } else if (d.status === 'failed') {
      if (polling) { clearInterval(polling); polling = null; }
      refreshLists();
    }
    if (d.runUrl) {
      $('jlink').classList.remove('hidden');
      $('jlink').innerHTML = '';
      var a = document.createElement('a');
      a.href = d.runUrl; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = '在 GitHub 上看这次运行的日志';
      a.style.color = '#6b7280';
      $('jlink').appendChild(a);
    }
  }).catch(function () {});
}

/** 状态文字 + 转圈图标（完成后停转） */
function setJobText(text, cls, spinning) {
  $('jtext').textContent = text;
  $('jstatus').className = 'status' + (cls ? ' ' + cls : '');
  $('jspin').style.display = spinning ? 'inline-block' : 'none';
}

/** 显示"共 x/y 步 · 已用 mm:ss"，本地每秒自增，不用一直问服务器 */
function tickClock(d) {
  var secs = elapsedBase;
  var step = (d && d.stepTotal > 0 && d.stepIndex > 0) ? ('第 ' + d.stepIndex + '/' + d.stepTotal + ' 步 · ') : '';
  var m = Math.floor(secs / 60), s = secs % 60;
  $('jstep').textContent = step + '已用 ' + m + ' 分 ' + (s < 10 ? '0' : '') + s + ' 秒';
}

function loadHistory() {
  api('/api/history').then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok || !d.jobs.length) return;
    var ul = $('hist');
    ul.innerHTML = '';
    d.jobs.forEach(function (j) {
      var li = document.createElement('li');
      var left = document.createElement('div');
      var t = document.createElement('div');
      t.textContent = j.name;
      var meta = document.createElement('div');
      meta.className = 'tag';
      meta.textContent = j.modeLabel + ' → ' + j.target + ' · ' +
        (j.status === 'done' ? '已完成' : j.status === 'failed' ? '失败' : '进行中');
      left.appendChild(t); left.appendChild(meta);
      li.appendChild(left);
      if (j.ready) {
        var a = document.createElement('a');
        a.href = '/api/download?id=' + j.id + '&password=' + encodeURIComponent(pw);
        a.textContent = '下载';
        a.style.color = '#0f9d58';
        li.appendChild(a);
      }
      ul.appendChild(li);
    });
  }).catch(function () {});
}

/** 任务完成后两个列表都要刷新（公共区可能多出刚译好的那条） */
function refreshLists() {
  loadHistory();
  if (role === 'vip' || role === 'admin') loadCommunity();
}

/** 时间戳 → 2025-09-29 18:03（本地时区，页面上的时间要跟人自己的表对得上） */
function fmtTime(ms) {
  if (!ms) return '—';
  var d = new Date(ms);
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function fmtSize(b) {
  if (!b) return '';
  return b < 1048576 ? Math.round(b / 1024) + ' KB' : (b / 1048576).toFixed(1) + ' MB';
}

/**
 * 公共文件区：别人译好的成品，VIP 及以上能下载。
 * 下载沿用 /api/download 直链 —— 同源导航浏览器会自动带上登录 Cookie，不用再拼口令。
 */
function loadCommunity() {
  api('/api/community').then(function (r) {
    return r.json().then(function (d) { return { status: r.status, d: d || {} }; },
                       function () { return { status: r.status, d: {} }; });
  }).then(function (res) {
    var d = res.d;
    // 掉登录 / 角色被降级：整块收起来，别杵在那儿报错
    if (res.status === 401 || res.status === 403) {
      $('commcard').classList.add('hidden');
      return;
    }
    // 服务端有话说就照说（比如本月额度用完了，402）—— 空表格看不出原因
    if (!d.ok) {
      $('commrows').innerHTML = '';
      $('commempty').classList.add('hidden');
      $('commnote').textContent = d.error || '公共文件区暂时打不开，稍后再试';
      return;
    }
    var rows = $('commrows');
    rows.innerHTML = '';
    if (!d.files || !d.files.length) {
      $('commempty').classList.remove('hidden');
      $('commnote').textContent = COMM_NOTE;
      return;
    }
    $('commempty').classList.add('hidden');
    $('commnote').textContent = d.truncated
      ? '只列了最近 ' + d.limit + ' 个文件，更早的没有显示。只显示 3 天内的译文。'
      : COMM_NOTE;
    d.files.forEach(function (f) {
      var tr = document.createElement('tr');

      var tdName = document.createElement('td');
      tdName.className = 'cname';
      tdName.textContent = f.name;
      var sz = fmtSize(f.size);
      if (sz) {
        var sub = document.createElement('div');
        sub.className = 'tag';
        sub.textContent = sz;
        tdName.appendChild(sub);
      }

      var tdMode = document.createElement('td');
      tdMode.textContent = (f.modeLabel || '') + (f.target ? ' → ' + f.target : '');

      var tdPages = document.createElement('td');
      tdPages.textContent = (f.pages === null || f.pages === undefined) ? '—' : f.pages;

      var tdUser = document.createElement('td');
      tdUser.textContent = f.uploader || '—';
      if (f.uploaderFull) tdUser.title = f.uploaderFull;   // 只有管理员拿得到全称

      var tdTime = document.createElement('td');
      tdTime.textContent = fmtTime(f.finishedAt);

      var tdDl = document.createElement('td');
      var a = document.createElement('a');
      a.className = 'cdl';
      a.href = '/api/download?id=' + encodeURIComponent(f.id);
      a.textContent = '下载';
      tdDl.appendChild(a);

      tr.appendChild(tdName); tr.appendChild(tdMode); tr.appendChild(tdPages);
      tr.appendChild(tdUser); tr.appendChild(tdTime); tr.appendChild(tdDl);
      rows.appendChild(tr);
    });
  }).catch(function () {});
}

if (pw) $('pw').value = pw;
checkAuth();
if (pw) setTimeout(function () { if (!authed) $('gobtn').click(); }, 600);
</script>
</body>
</html>
`;
