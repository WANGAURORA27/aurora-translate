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
  .hidden { display:none !important; }
  .bar { height:8px; background:#eef1f6; border-radius:99px; overflow:hidden; margin:14px 0 8px; }
  .bar > i { display:block; height:100%; width:0; background:var(--brand); transition:width .25s; }
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
  code { background:#f1f3f7; padding:2px 6px; border-radius:5px; font-size:13px; }
  .tip { font-size:13px; color:var(--sub); margin-top:10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Aurora 文档翻译</h1>
  <p class="lead">上传 PDF / Word，保持原来的排版把文字换成中文，公式和图表都会留着。</p>

  <div class="card" id="gate">
    <h2>请输入使用口令</h2>
    <input type="password" id="pw" placeholder="口令" autocomplete="current-password">
    <p class="tip" id="gatem">口令由站点主人提供，用来防止陌生人消耗翻译额度。</p>
    <div style="margin-top:14px"><button id="gobtn">进入</button></div>
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
      <div style="margin-top:16px"><button id="upbtn">开始翻译</button></div>
      <div class="bar hidden" id="ubar"><i></i></div>
      <p class="tip" id="upnote">单个文件最大 95MB。30 页大约 1 分钟，几百页的教材会久一些。译文保留 3 天，请及时下载。</p>
    </div>

    <div class="card hidden" id="jobcard">
      <h2>翻译进度</h2>
      <p class="status" id="jstatus">排队中</p>
      <p class="muted" id="jname"></p>
      <p class="muted" id="jnote"></p>
      <div class="bar"><i id="jbar"></i></div>
      <p class="muted" id="jstats"></p>
      <a class="dl hidden" id="jdl" href="#">下载译文</a>
      <p class="tip hidden" id="jlink"></p>
    </div>

    <div class="card">
      <h2>最近的任务</h2>
      <ul class="hist" id="hist"><li class="muted">暂无记录</li></ul>
    </div>
  </div>
</div>

<script>
var pw = sessionStorage.getItem('aurora_pw') || '';
var polling = null;

function $(id) { return document.getElementById(id); }

function say(id, text, cls) {
  var el = $(id);
  el.textContent = text;
  el.className = cls ? ('status ' + cls) : (id === 'jstatus' ? 'status' : el.className);
}

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ 'x-password': pw }, opts.headers || {});
  return fetch(path, opts);
}

$('gobtn').onclick = function () {
  pw = $('pw').value.trim();
  if (!pw) { $('gatem').textContent = '先填口令'; return; }
  fetch('/api/verify', { headers: { 'x-password': pw } }).then(function (r) {
    if (!r.ok) { $('gatem').textContent = '口令不对，再试一次'; return; }
    sessionStorage.setItem('aurora_pw', pw);
    $('gate').classList.add('hidden');
    $('app').classList.remove('hidden');
    loadHistory();
  }).catch(function () { $('gatem').textContent = '连不上服务器，稍后再试'; });
};
$('pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('gobtn').click(); });

$('upbtn').onclick = function () {
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
  $('jobcard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function poll(id) {
  api('/api/status?id=' + id).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { say('jstatus', d.error || '查询失败', 'failed'); return; }
    var label = d.status === 'done' ? '翻译完成' : d.status === 'failed' ? '翻译失败'
              : d.status === 'running' ? '正在翻译…' : '排队中…';
    say('jstatus', label, d.status === 'done' ? 'done' : d.status === 'failed' ? 'failed' : '');
    $('jnote').textContent = d.note || '';
    $('jbar').style.width = d.status === 'done' ? '100%' : d.status === 'running' ? '62%' : '12%';
    if (d.stats) {
      var s = d.stats;
      var bits = [];
      if (s.pages) bits.push(s.pages + ' 页');
      if (s.lines) bits.push(s.lines + ' 段');
      if (s.seconds) bits.push(Math.round(s.seconds) + ' 秒');
      if (s.chars) bits.push(s.chars + ' 字');
      $('jstats').textContent = bits.join(' · ');
    }
    if (d.ready) {
      $('jdl').classList.remove('hidden');
      if (polling) { clearInterval(polling); polling = null; }
      loadHistory();
    } else if (d.status === 'failed') {
      if (polling) { clearInterval(polling); polling = null; }
      loadHistory();
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

if (pw) { $('pw').value = pw; $('gobtn').click(); }
</script>
</body>
</html>
`;
