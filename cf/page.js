/**
 * 页面本身（单文件，无外部依赖，Worker 直接把它吐给浏览器）
 * 注意：这里是模板字符串，里面不要出现反引号和 ${，内联脚本统一用单引号拼接。
 * 生成：页面源文件里的反斜杠必须先翻倍再嵌进来，否则 \. 这类转义会被吃掉。
 */

export const PAGE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aurora 文档翻译 · 保持版式的 PDF / Word 翻译</title>
<meta name="description" content="上传 PDF 或 Word，保持原来的排版把文字换成中文，公式、表格和图表都留在原位。支持中英对照与扫描件 OCR。">
<meta name="theme-color" content="#2F6DF6">
<link rel="canonical" href="https://doc.ourmetaverse.cn/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Aurora 文档翻译">
<meta property="og:title" content="Aurora 文档翻译 · 保持版式的 PDF / Word 翻译">
<meta property="og:description" content="上传 PDF 或 Word，保持原来的排版把文字换成中文，公式、表格和图表都留在原位。">
<meta property="og:url" content="https://doc.ourmetaverse.cn/">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Aurora 文档翻译">
<meta name="twitter:description" content="保持版式的 PDF / Word 翻译，公式与图表留在原位。">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%231FC7B6'/%3E%3Cstop offset='.5' stop-color='%234F7BF7'/%3E%3Cstop offset='1' stop-color='%238B5CF6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='32' height='32' rx='8' fill='%230E1018'/%3E%3Cpath d='M5 21c4-9 18-9 22 0' fill='none' stroke='url(%23g)' stroke-width='3.5' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
  :root {
    color-scheme: light;
    --canvas:#F5F7FB; --surface:#FFFFFF;
    --ink:#14161C; --ink-2:#575E72; --ink-3:#8B92A3;
    --line:#E5E9F1; --line-strong:#D3D9E6;
    --brand:#2F6DF6; --brand-ink:#1E4FD0; --brand-tint:#EDF2FF; --brand-on:#FFFFFF;
    --ok:#0B8A4B; --ok-tint:#E9F7EF; --bad:#C8341F; --bad-tint:#FDEEEA;
    /* Aurora：全局唯一的重色，只给进度用 */
    --a1:#1FC7B6; --a2:#4F7BF7; --a3:#8B5CF6;
    --radius:12px; --radius-sm:8px;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --canvas:#0E1018; --surface:#161923;
      --ink:#EDEFF5; --ink-2:#A6ADC0; --ink-3:#767D91;
      --line:#252A38; --line-strong:#333A4D;
      --brand:#5B8CFF; --brand-ink:#8AB0FF; --brand-tint:#1A2340; --brand-on:#0B0D14;
      --ok:#3DD68C; --ok-tint:#12261C; --bad:#FF6B5A; --bad-tint:#2A1614;
    }
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0; background:var(--canvas); color:var(--ink);
    font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  }
  a { color:var(--brand-ink); }
  :focus-visible { outline:2px solid var(--brand); outline-offset:2px; border-radius:4px; }

  /* ---------- 顶栏 ---------- */
  .topbar { border-bottom:1px solid var(--line); background:var(--surface); }
  .topbar .inner { max-width:1080px; margin:0 auto; padding:14px 20px;
                   display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .mark { display:flex; align-items:baseline; gap:9px; min-width:0; }
  .mark .name { font-size:19px; font-weight:700; letter-spacing:.2px; }
  .mark .cn { font-size:14px; color:var(--ink-2); }
  .acct { font-size:13px; color:var(--ink-2); text-align:right; line-height:1.45; }
  .acct b { color:var(--ink); font-weight:600; }
  .acct .quota { font-family:var(--mono); font-size:12px; }

  /* ---------- 外壳 ---------- */
  .shell { max-width:1080px; margin:0 auto; padding:28px 20px 72px; }
  .grid { display:grid; grid-template-columns:minmax(0,1.32fr) minmax(0,1fr); gap:28px; align-items:start; }
  @media (max-width: 940px) { .grid { grid-template-columns:1fr; gap:20px; } }

  h1 { font-size:26px; line-height:1.3; margin:0 0 8px; letter-spacing:-.2px; }
  .lead { color:var(--ink-2); font-size:14px; margin:0 0 22px; max-width:56ch; }
  h2 { font-size:13px; font-weight:600; color:var(--ink-2); margin:0 0 12px; letter-spacing:.3px; }

  .panel { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:20px; }
  .panel + .panel, .stack > * + * { margin-top:16px; }
  .stack { display:block; }
  /* 公共文件整页宽，跟上面的双栏拉开距离 */
  #commcard { margin-top:20px; }

  /* ---------- 拖入区（主视觉） ---------- */
  .drop {
    display:block; width:100%; text-align:center; cursor:pointer;
    border:1.5px dashed var(--line-strong); border-radius:14px;
    background:var(--surface); padding:36px 22px; transition:border-color .15s, background .15s;
  }
  .drop:hover { border-color:var(--brand); background:var(--brand-tint); }
  .drop.over { border-color:var(--brand); border-style:solid; background:var(--brand-tint); }
  .drop .icon { width:38px; height:38px; margin:0 auto 12px; display:block; }
  .drop .big { font-size:16px; font-weight:600; }
  .drop .sub { font-size:13px; color:var(--ink-2); margin-top:5px; }
  .drop .file { font-family:var(--mono); font-size:12.5px; color:var(--brand-ink);
                margin-top:9px; word-break:break-all; }
  .sr { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
        clip:rect(0 0 0 0); white-space:nowrap; border:0; }

  /* ---------- 选项 ---------- */
  .field { margin-top:20px; }
  .field > .lbl { font-size:12.5px; color:var(--ink-2); margin-bottom:8px; display:block; }
  .modes { display:flex; flex-direction:column; gap:7px; }
  .mode { display:flex; gap:11px; align-items:flex-start; padding:11px 13px;
          border:1px solid var(--line); border-radius:var(--radius-sm); cursor:pointer; transition:border-color .13s, background .13s; }
  .mode:hover { border-color:var(--line-strong); }
  .mode input { margin:3px 0 0; accent-color:var(--brand); flex:none; }
  .mode .t { font-size:14px; font-weight:600; }
  .mode .d { font-size:12.5px; color:var(--ink-2); }
  .mode:has(input:checked) { border-color:var(--brand); background:var(--brand-tint); }
  .mode:has(input:focus-visible) { outline:2px solid var(--brand); outline-offset:1px; }

  .chips { display:flex; flex-wrap:wrap; gap:7px; }
  .chip { position:relative; }
  .chip input { position:absolute; opacity:0; inset:0; cursor:pointer; }
  .chip span { display:inline-block; padding:7px 14px; border:1px solid var(--line);
               border-radius:999px; font-size:13.5px; color:var(--ink-2); transition:.13s; }
  .chip:hover span { border-color:var(--line-strong); color:var(--ink); }
  .chip input:checked + span { background:var(--brand); border-color:var(--brand); color:var(--brand-on); font-weight:600; }
  .chip input:focus-visible + span { outline:2px solid var(--brand); outline-offset:2px; }

  /* 「精修」：保持两个字，解释放在 title 里，不占版面 */
  .refine { display:flex; align-items:center; gap:8px; margin-top:16px; font-size:14px; cursor:pointer; width:max-content; }
  .refine input { accent-color:var(--brand); margin:0; flex:none; }

  .primary { width:100%; margin-top:20px; background:var(--brand); color:var(--brand-on); border:0;
             border-radius:var(--radius-sm); padding:13px 18px; font-size:15px; font-weight:600;
             font-family:inherit; cursor:pointer; transition:background .13s; }
  .primary:hover:not(:disabled) { background:var(--brand-ink); }
  .primary:disabled { opacity:.5; cursor:not-allowed; }

  .note { font-size:12.5px; color:var(--ink-3); margin:12px 0 0; min-height:1.2em; }
  .note.err { color:var(--bad); }

  /* ---------- 极光进度带 ---------- */
  .band { position:relative; height:6px; border-radius:999px; background:var(--line); overflow:hidden; margin:14px 0 10px; }
  .band > i { display:block; height:100%; width:0; border-radius:999px;
              background:linear-gradient(90deg,var(--a1),var(--a2),var(--a3)); transition:width .4s ease; }
  .band.unknown > i { width:38%; animation:aurora-slide 1.5s ease-in-out infinite; }
  @keyframes aurora-slide { 0%{margin-left:-38%} 100%{margin-left:100%} }

  .rowbetween { display:flex; align-items:baseline; justify-content:space-between; gap:12px; }
  .state { font-size:14px; font-weight:600; display:flex; align-items:center; gap:7px; }
  .state.done { color:var(--ok); }
  .state.failed { color:var(--bad); }
  .clock { font-family:var(--mono); font-size:12px; color:var(--ink-3); }
  .jobname { font-size:13px; color:var(--ink-2); word-break:break-all; margin:2px 0 0; }
  .stats { font-family:var(--mono); font-size:11.5px; color:var(--ink-3); margin:8px 0 0; }

  .dl { display:inline-flex; align-items:center; gap:7px; margin-top:14px; background:var(--ok);
        color:#fff; text-decoration:none; padding:10px 16px; border-radius:var(--radius-sm); font-size:14px; font-weight:600; }
  .dl:hover { filter:brightness(.95); }

  /* ---------- 列表 ---------- */
  ul.hist { list-style:none; margin:0; padding:0; }
  ul.hist li { display:flex; justify-content:space-between; gap:12px; align-items:center;
               padding:11px 0; border-top:1px solid var(--line); font-size:14px; }
  ul.hist li:first-child { border-top:0; padding-top:0; }
  .hist .nm { min-width:0; word-break:break-all; }
  .tag { font-size:12px; color:var(--ink-3); margin-top:2px; }
  .acts { flex:none; }
  .acts a { white-space:nowrap; font-size:13px; }
  .acts a + a { margin-left:12px; color:var(--ink-2); }
  .empty { font-size:13px; color:var(--ink-3); margin:0; }

  .tablewrap { overflow-x:auto; -webkit-overflow-scrolling:touch; margin:0 -20px; padding:0 20px; }
  table.comm { width:100%; border-collapse:collapse; font-size:13.5px; min-width:680px; }
  table.comm th { text-align:left; font-weight:500; color:var(--ink-3); font-size:11.5px;
                  padding:0 12px 8px 0; border-bottom:1px solid var(--line); white-space:nowrap; }
  table.comm td { padding:10px 12px 10px 0; border-top:1px solid var(--line); vertical-align:top; }
  table.comm tr:first-child td { border-top:0; }
  table.comm td.cname { max-width:230px; word-break:break-all; }
  table.comm th:last-child, table.comm td:last-child { padding-right:0; }
  table.comm a.cdl { white-space:nowrap; color:var(--ok); text-decoration:none; }

  code { background:var(--brand-tint); padding:2px 6px; border-radius:5px; font-family:var(--mono); font-size:12.5px; }

  /* ---------- 登录卡 ---------- */
  .signin { max-width:480px; margin:6vh auto 0; }
  .signin h1 { font-size:22px; }
  .gateway { margin-top:18px; }
  .gateway summary { font-size:13px; color:var(--ink-2); cursor:pointer; }
  .gateway input { width:100%; margin-top:10px; padding:10px 12px; font-family:inherit; font-size:14px;
                   border:1px solid var(--line); border-radius:var(--radius-sm); background:var(--surface); color:var(--ink); }
  .ghost { margin-top:10px; background:var(--surface); color:var(--ink); border:1px solid var(--line-strong);
           border-radius:var(--radius-sm); padding:10px 16px; font-size:14px; font-family:inherit; cursor:pointer; }
  .ghost:hover { border-color:var(--brand); color:var(--brand-ink); }

  .hidden { display:none !important; }

  /* 顶栏小 spinner（登录检查用） */
  .spin { display:inline-block; width:11px; height:11px; border:2px solid var(--line-strong);
          border-top-color:var(--brand); border-radius:50%; animation:aurora-spin .9s linear infinite; vertical-align:-1px; }
  @keyframes aurora-spin { to { transform:rotate(360deg); } }

  @media (prefers-reduced-motion: reduce) {
    * { animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important; }
    .band.unknown > i { width:100%; }
  }
</style>
</head>
<body>

<div class="topbar">
  <div class="inner">
    <div class="mark">
      <span class="name">Aurora</span><span class="cn">文档翻译</span>
    </div>
    <div class="acct" id="acct" aria-live="polite">
      <span id="acctstate"><span class="spin"></span> 正在检查登录状态…</span>
      <div class="quota" id="acctquota"></div>
    </div>
  </div>
</div>

<!-- 未登录：登录卡 -->
<section class="shell signin" id="signin" aria-labelledby="signin-h">
  <h1 id="signin-h">保持版式的文档翻译</h1>
  <p class="lead">上传 PDF 或 Word，把文字换成中文，公式、表格和图表留在原位。</p>
  <div class="panel">
    <p class="note" id="signinmsg" style="margin-top:0">翻译需要先登录 —— 每个人的额度单独计算，互不影响。</p>
    <p style="margin:14px 0 0">
      <a class="dl" href="https://account.ourmetaverse.cn/" target="_blank" rel="noopener">去登录 / 注册</a>
    </p>
    <p class="note">没有账号？注册只需一个邮箱收验证码。登录后回到本页即可开始翻译。</p>
    <details class="gateway">
      <summary>管理员 / 自动化：改用管理口令</summary>
      <!-- 管理口令是应急通道，不占任何人的额度 -->
      <input type="password" id="pw" placeholder="管理口令" autocomplete="current-password">
      <div><button type="button" class="ghost" id="gobtn">用口令进入</button></div>
      <p class="note" id="gatem">管理口令是应急通道，不占任何人的额度。</p>
    </details>
  </div>
</section>

<!-- 已登录：工作台 -->
<main class="shell hidden" id="app">
 <div class="grid">
  <!-- 左：工作区 -->
  <div class="stack">
    <div class="panel">
      <h2>上传文件</h2>

      <label class="drop" id="drop" for="file">
        <svg class="icon" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
          <defs><linearGradient id="ag" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#1FC7B6"/><stop offset=".5" stop-color="#4F7BF7"/><stop offset="1" stop-color="#8B5CF6"/>
          </linearGradient></defs>
          <path d="M5 21c4-9 18-9 22 0" fill="none" stroke="url(#ag)" stroke-width="3" stroke-linecap="round"/>
          <path d="M16 11V4m0 0-3.4 3.4M16 4l3.4 3.4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity=".55"/>
        </svg>
        <div class="big">把文件拖到这里，或点击选择</div>
        <div class="sub">支持 PDF、Word（.docx / .doc），单个最大 95MB</div>
        <div class="file hidden" id="filename"></div>
        <input type="file" id="file" class="sr" accept=".pdf,.docx,.doc">
      </label>

      <fieldset class="field" style="border:0;padding:0;margin:20px 0 0">
        <legend class="lbl">输出形式</legend>
        <div class="modes">
          <label class="mode">
            <input type="radio" name="mode" value="inplace" checked>
            <span><span class="t">保持版式</span><br><span class="d">中文替换原文，版式不变 —— 大多数论文、教材选这个。</span></span>
          </label>
          <label class="mode">
            <input type="radio" name="mode" value="bilingual">
            <span><span class="t">中英对照</span><br><span class="d">左右两栏逐段对照，适合精读和校对。</span></span>
          </label>
          <label class="mode">
            <input type="radio" name="mode" value="ocr">
            <span><span class="t">扫描件 OCR</span><br><span class="d">图片型 PDF 先识别文字再翻译，耗时更长。</span></span>
          </label>
        </div>
      </fieldset>

      <div class="field">
        <span class="lbl" id="tgtlbl">目标语言</span>
        <div class="chips" role="radiogroup" aria-labelledby="tgtlbl" id="targets"></div>
      </div>

      <!-- 精修：默认不勾；只有 can_refine 的账号才显示（见 checkAuth） -->
      <label class="refine hidden" id="refinebox" title="翻完后再通读润色一遍，更通顺，耗时更长">
        <input type="checkbox" id="refine"><span>精修</span>
      </label>

      <button type="button" class="primary" id="upbtn">开始翻译</button>
      <p class="note" id="upnote">译文保留 3 天，请及时下载。30 页大约 1 分钟，几百页的教材会久一些。</p>
    </div>
  </div>

  <!-- 右：动态 -->
  <div class="stack">
    <div class="panel hidden" id="jobcard">
      <h2>翻译进度</h2>
      <div class="rowbetween">
        <span class="state" id="jstatus" role="status" aria-live="polite">
          <span class="spin" id="jspin"></span><span id="jtext">排队中</span>
        </span>
        <span class="clock" id="jclock">已用 0 分 00 秒</span>
      </div>
      <p class="jobname" id="jname"></p>
      <div class="band" id="jbandwrap"><i id="jbar"></i></div>
      <p class="note" id="jstep"></p>
      <p class="note" id="jnote"></p>
      <p class="stats" id="jstats"></p>
      <a class="dl hidden" id="jdl" href="#">下载译文</a>
      <p class="note hidden" id="jlink"></p>
    </div>

    <div class="panel">
      <h2>最近的任务</h2>
      <ul class="hist" id="hist"><li class="empty">还没有翻译记录</li></ul>
    </div>

 </div><!-- /右栏 -->
 </div><!-- /grid -->

 <!-- 公共文件区：只有 VIP / 管理员可见（见 checkAuth；服务端 /api/community 还会再判一次角色） -->
 <!-- 七列表格塞进窄侧栏只会被横向滚动挤扁，所以让它占满整页宽 -->
 <div class="panel hidden" id="commcard">
  <h2>公共文件</h2>
  <p class="note" id="commnote" style="margin:0 0 12px">别人翻译好的文件，VIP 及以上可以直接下载。只显示 3 天内的译文。</p>
  <div class="tablewrap">
   <table class="comm">
    <thead>
     <tr><th>文件名</th><th>输出形式</th><th>页数</th><th>上传者</th><th>完成时间</th><th>译文</th><th>原件</th></tr>
    </thead>
    <tbody id="commrows"></tbody>
   </table>
  </div>
  <p class="empty hidden" id="commempty">还没有别人翻译过的文件</p>
 </div>
</main>

<script>
var pw = sessionStorage.getItem('aurora_pw') || '';
var authed = false;          // 已通过登录或管理口令验证
var canRefine = false;       // 当前账号有没有「精修」能力（由 /api/me 决定）
var role = '';               // 当前账号角色：user / vip / admin（公共文件区是否可见看它）
var polling = null;
var elapsedBase = 0;         // 服务端给的已用秒数
var lastJob = null;          // 最近一次状态，供本地秒表使用
var clockTimer = null;
var currentJob = null;       // 正在轮询的任务，切回标签页时接着问

// 公共区只列 3 天内的译文（译文存储只留 3 天，更早的点下载只会 410），说明文案跟服务端一致
var COMM_NOTE = '别人翻译好的文件，VIP 及以上可以直接下载。只显示 3 天内的译文。';
var TARGETS = ['中文','英文','日文','韩文','法文','德文','西班牙文','俄文'];
var MAXBYTES = 95 * 1024 * 1024;
var OKEXT = /\\.(pdf|docx?)$/i;

function $(id) { return document.getElementById(id); }

/** 目标语言做成一行 chips：8 个选项下比下拉框少一次点击，也看得见全部选项 */
(function buildTargets() {
  var box = $('targets');
  TARGETS.forEach(function (lang, i) {
    var lab = document.createElement('label');
    lab.className = 'chip';
    var input = document.createElement('input');
    input.type = 'radio'; input.name = 'target'; input.value = lang;
    if (i === 0) input.checked = true;
    var span = document.createElement('span');
    span.textContent = lang;
    lab.appendChild(input); lab.appendChild(span);
    box.appendChild(lab);
  });
})();

function pickedMode() {
  var r = document.querySelector('input[name=mode]:checked');
  return r ? r.value : 'inplace';
}
function pickedTarget() {
  var r = document.querySelector('input[name=target]:checked');
  return r ? r.value : '中文';
}
function modeLabel(m) {
  return m === 'bilingual' ? '中英对照' : m === 'ocr' ? '扫描件 OCR' : '保持版式';
}

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ 'x-password': pw }, opts.headers || {});
  return fetch(path, opts);
}

/** 本地秒表：每秒钟把「已用时间」往上加，不用一直去问服务器 */
function startClock() {
  if (clockTimer) return;
  clockTimer = setInterval(function () {
    if (!lastJob) return;
    if (lastJob.status !== 'running' && lastJob.status !== 'queued') return;
    elapsedBase += 1;
    tickClock(lastJob);
  }, 1000);
}

/** 显示可用的上传界面 */
function showApp() {
  $('signin').classList.add('hidden');
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
      $('acctstate').innerHTML = '<b></b>' + tag;
      $('acctstate').querySelector('b').textContent = d.user.email;
      $('acctquota').textContent = d.quota.unlimited
        ? '额度：不限'
        : '本月剩余 ' + d.quota.remaining + ' / ' + d.quota.quota + ' 页（已用 ' + d.quota.used + '）';
      // 精修只有账号带能力位时才给看；服务端还会再校验一遍（前端藏起来不是权限）
      canRefine = d.user.can_refine === 1;
      if (canRefine) $('refinebox').classList.remove('hidden');
      // 公共文件区：VIP 起可见。藏起来只是不碍眼，真正的门在服务端
      if (role === 'vip' || role === 'admin') $('commcard').classList.remove('hidden');
      showApp();
      return;
    }
    authed = false; canRefine = false; role = '';
    $('refinebox').classList.add('hidden');
    $('commcard').classList.add('hidden');
    $('acctstate').textContent = '还没有登录';
  }).catch(function () {
    $('acctstate').textContent = '连不上服务器，稍后再试';
  });
}

/** 管理口令通道（自动化自检 / 应急） */
$('gobtn').onclick = function () {
  var v = $('pw').value.trim();
  if (!v) { $('gatem').textContent = '先填口令'; return; }
  fetch('/api/verify', { headers: { 'x-password': v } }).then(function (r) {
    if (!r.ok) { $('gatem').textContent = '口令不对，再试一次'; return; }
    pw = v;
    sessionStorage.setItem('aurora_pw', pw);
    authed = true;
    showApp();
  }).catch(function () { $('gatem').textContent = '连不上服务器，稍后再试'; });
};
$('pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('gobtn').click(); });

/* ---------- 选文件：拖入 + 点击 + 上传前先校验 ---------- */
var drop = $('drop');
function showPick(f) {
  if (!f) { $('filename').classList.add('hidden'); return; }
  $('filename').textContent = f.name + ' · ' + fmtSize(f.size);
  $('filename').classList.remove('hidden');
}
function checkFile(f) {
  if (!OKEXT.test(f.name)) return '只支持 PDF 和 Word（.docx / .doc）';
  if (f.size > MAXBYTES) return '文件超过 95MB，请先压缩或拆分';
  return '';
}
$('file').addEventListener('change', function () {
  var f = $('file').files[0];
  if (!f) return;
  var bad = checkFile(f);
  $('upnote').className = bad ? 'note err' : 'note';
  $('upnote').textContent = bad || '译文保留 3 天，请及时下载。';
  showPick(bad ? null : f);
});
['dragenter', 'dragover'].forEach(function (ev) {
  drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
});
['dragleave', 'drop'].forEach(function (ev) {
  drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
});
drop.addEventListener('drop', function (e) {
  var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  var bad = checkFile(f);
  if (bad) {
    $('upnote').className = 'note err';
    $('upnote').textContent = bad;
    return;
  }
  // 把拖进来的文件塞进原生 input，后续逻辑只认 input.files[0]，不开两条路径
  var dt = new DataTransfer();
  dt.items.add(f);
  $('file').files = dt.files;
  $('upnote').className = 'note';
  $('upnote').textContent = '译文保留 3 天，请及时下载。';
  showPick(f);
});

$('upbtn').onclick = function () {
  if (!authed) { setNote('请先登录（或输入管理口令）', true); return; }
  var f = $('file').files[0];
  if (!f) { setNote('先选一个文件', true); return; }
  var bad = checkFile(f);
  if (bad) { setNote(bad, true); return; }

  $('upbtn').disabled = true;
  setNote('正在上传…');

  var xhr = new XMLHttpRequest();
  xhr.open('PUT', '/api/upload');
  xhr.setRequestHeader('x-password', pw);
  xhr.setRequestHeader('x-filename', encodeURIComponent(f.name));
  xhr.setRequestHeader('x-mode', pickedMode());
  xhr.setRequestHeader('x-target', encodeURIComponent(pickedTarget()));
  // 明确送 0/1：服务端据 can_refine 复核，前端传来的 1 不算数
  xhr.setRequestHeader('x-refine', (canRefine && $('refine').checked) ? '1' : '0');
  xhr.upload.onprogress = function (e) {
    if (e.lengthComputable) {
      setNote('正在上传 ' + (e.loaded / 1048576).toFixed(1) + ' / ' + (e.total / 1048576).toFixed(1) + ' MB');
    }
  };
  xhr.onload = function () {
    $('upbtn').disabled = false;
    var data = {};
    try { data = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status !== 200 || !data.ok) {
      setNote(data.error || ('上传失败（' + xhr.status + '）'), true);
      return;
    }
    setNote('上传完成，已交给后台翻译。');
    $('file').value = '';
    showPick(null);
    watch(data.id, data.name, data.mode, data.target);
  };
  xhr.onerror = function () {
    $('upbtn').disabled = false;
    setNote('网络中断，上传失败', true);
  };
  xhr.send(f);
};

function setNote(text, isErr) {
  $('upnote').className = isErr ? 'note err' : 'note';
  $('upnote').textContent = text;
}

function watch(id, name, mode, target) {
  currentJob = id;
  $('jobcard').classList.remove('hidden');
  $('jname').textContent = name + ' · ' + modeLabel(mode) + ' → ' + target;
  $('jdl').classList.add('hidden');
  $('jlink').classList.add('hidden');
  $('jdl').href = '/api/download?id=' + id + '&password=' + encodeURIComponent(pw);
  $('jdl').setAttribute('download', '');
  startPolling();
  startClock();
  $('jobcard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function startPolling() {
  if (polling || !currentJob) return;
  poll(currentJob);
  polling = setInterval(function () { poll(currentJob); }, 4000);
}
function stopPolling() {
  if (polling) { clearInterval(polling); polling = null; }
}
// 切到后台就别再问了，回来立刻补一次 —— 省电，也省服务端
document.addEventListener('visibilitychange', function () {
  if (document.hidden) stopPolling();
  else if (currentJob && lastJob && (lastJob.status === 'running' || lastJob.status === 'queued')) startPolling();
});

function poll(id) {
  api('/api/status?id=' + id).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { setJobText(d.error || '查询失败', 'failed', false); stopPolling(); return; }
    lastJob = d;
    var running = d.status === 'running' || d.status === 'queued';
    setJobText(d.phase || (d.status === 'done' ? '翻译完成' : d.status === 'failed' ? '翻译失败' : '处理中…'),
               d.status === 'done' ? 'done' : d.status === 'failed' ? 'failed' : '', running);

    // 有「第几步/共几步」就用真实比例；没有就把进度带切成不确定态的流动动画，
    // 不假装一个百分比出来
    if (d.status === 'done') { setBand(100, false); }
    else if (d.stepTotal > 0 && d.stepIndex > 0) { setBand(Math.min(97, Math.round(d.stepIndex / d.stepTotal * 100)), false); }
    else if (d.status === 'running') { setBand(0, true); }
    else { setBand(4, false); }

    elapsedBase = d.elapsedSec || 0;
    tickClock(d);
    $('jnote').textContent = d.note || '';
    if (d.stats) {
      var s = d.stats, bits = [];
      if (s.pages) bits.push(s.pages + ' 页');
      if (s.units) bits.push(s.units + ' 段');
      if (s.api_calls) bits.push('调用 ' + s.api_calls + ' 次');
      if (s.api_tokens_in) bits.push('入 ' + s.api_tokens_in + ' tokens');
      if (s.seconds) bits.push('耗时 ' + Math.round(s.seconds) + ' 秒');
      if (s.outputMB) bits.push(s.outputMB + ' MB');
      $('jstats').textContent = bits.join(' · ');
    } else {
      $('jstats').textContent = '';
    }
    if (d.ready) {
      $('jdl').classList.remove('hidden');
      stopPolling(); currentJob = null;
      refreshLists();
    } else if (d.status === 'failed') {
      stopPolling(); currentJob = null;
      refreshLists();
    }
    if (d.runUrl) {
      $('jlink').classList.remove('hidden');
      $('jlink').innerHTML = '';
      var a = document.createElement('a');
      a.href = d.runUrl; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = '在 GitHub 上看这次运行的日志';
      $('jlink').appendChild(a);
    }
  }).catch(function () { /* 网络抖动就跳过这一轮，下一轮继续 */ });
}

function setBand(pct, unknown) {
  var wrap = $('jbandwrap');
  wrap.classList.toggle('unknown', !!unknown);
  $('jbar').style.width = unknown ? '' : pct + '%';
}

/** 状态文字 + 转圈图标（完成后停转） */
function setJobText(text, cls, spinning) {
  $('jtext').textContent = text;
  $('jstatus').className = 'state' + (cls ? ' ' + cls : '');
  $('jspin').style.display = spinning ? 'inline-block' : 'none';
}

/** 显示「第 x/y 步 · 已用 mm:ss」，本地每秒自增，不用一直问服务器 */
function tickClock(d) {
  var secs = elapsedBase;
  var step = (d && d.stepTotal > 0 && d.stepIndex > 0) ? ('第 ' + d.stepIndex + '/' + d.stepTotal + ' 步 · ') : '';
  var m = Math.floor(secs / 60), s = secs % 60;
  $('jclock').textContent = '已用 ' + m + ' 分 ' + (s < 10 ? '0' : '') + s + ' 秒';
  $('jstep').textContent = step ? (step.replace(/ · $/, '')) : '';
}

/** 造一个下载链接（原文/原件共用，样式跟现有「下载」保持一致） */
function dlLink(href, text, color) {
  var a = document.createElement('a');
  a.href = href;
  a.textContent = text;
  if (color) a.style.color = color;
  return a;
}

function loadHistory() {
  api('/api/history').then(function (r) { return r.json(); }).then(function (d) {
    var ul = $('hist');
    ul.innerHTML = '';
    if (!d.ok || !d.jobs.length) {
      var li = document.createElement('li');
      li.className = 'empty';
      li.textContent = '还没有翻译记录';
      ul.appendChild(li);
      return;
    }
    d.jobs.forEach(function (j) {
      var li = document.createElement('li');
      var left = document.createElement('div');
      left.className = 'nm';
      var t = document.createElement('div');
      t.textContent = j.name;
      var meta = document.createElement('div');
      meta.className = 'tag';
      meta.textContent = j.modeLabel + ' → ' + j.target + ' · ' +
        (j.status === 'done' ? '已完成' : j.status === 'failed' ? '失败' : '进行中');
      left.appendChild(t); left.appendChild(meta);
      li.appendChild(left);
      // 两个链接放一个容器里，别让 flex 的 space-between 把它们拆到两头
      var acts = document.createElement('div');
      acts.className = 'acts';
      if (j.ready) {
        acts.appendChild(dlLink('/api/download?id=' + j.id + '&password=' + encodeURIComponent(pw), '下载', '#0B8A4B'));
      }
      // 原件：上传后就一直在（不管译文好没好），有记录才显示
      if (j.hasInput) {
        acts.appendChild(dlLink('/api/original?id=' + j.id + '&password=' + encodeURIComponent(pw), '原件'));
      }
      if (acts.children.length) li.appendChild(acts);
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
      var a = dlLink('/api/download?id=' + encodeURIComponent(f.id), '下载', '');
      a.className = 'cdl';
      tdDl.appendChild(a);

      // 原件：VIP/管理员同样能下别人的（服务端 /api/original 再判一次）
      var tdSrc = document.createElement('td');
      if (f.hasInput) {
        var b = dlLink('/api/original?id=' + encodeURIComponent(f.id), '原件', '#8B92A3');
        b.className = 'cdl';
        tdSrc.appendChild(b);
      } else {
        tdSrc.textContent = '—';
      }

      tr.appendChild(tdName); tr.appendChild(tdMode); tr.appendChild(tdPages);
      tr.appendChild(tdUser); tr.appendChild(tdTime); tr.appendChild(tdDl);
      tr.appendChild(tdSrc);
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
