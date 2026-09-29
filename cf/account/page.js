/**
 * 账户系统页面（单文件，Worker 直接吐给浏览器）
 * 注意：这是模板字符串，内部不要出现反引号和 ${，内联脚本一律用单引号拼接。
 *      —— 也因此内联脚本里**不要写反斜杠转义**（模板字符串会先吃一层），需要换行用 String.fromCharCode(10)。
 */

export const PAGE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aurora 账户</title>
<style>
  :root {
    --ink:#16181d; --sub:#6b7280; --line:#e5e7eb; --brand:#2f6df6;
    --brand-soft:#eaf1ff; --ok:#0f9d58; --ok-soft:#e8f5ee;
    --bad:#d93025; --bad-soft:#fdecef; --warn:#b06000; --warn-soft:#fff4e5;
    --soft:#f1f3f7; --bg:#f6f7f9;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
         color:var(--ink); background:var(--bg); -webkit-text-size-adjust:100%; }
  .wrap { max-width:520px; margin:0 auto; padding:36px 20px 64px; }
  .wrap.wide { max-width:1120px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .lead { color:var(--sub); font-size:14px; margin:0 0 20px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:22px; margin-bottom:14px; }
  label { display:block; font-size:13px; color:var(--sub); margin:14px 0 6px; }
  input { width:100%; padding:11px 12px; border:1px solid var(--line); border-radius:8px; font-size:15px; }
  input:focus { outline:2px solid #cfe0ff; border-color:var(--brand); }
  button { background:var(--brand); color:#fff; border:0; border-radius:8px; padding:12px 18px;
           font-size:15px; cursor:pointer; }
  button:disabled { background:#b9c3d6; cursor:not-allowed; }
  button.ghost { background:#fff; color:var(--ink); border:1px solid var(--line); }
  button.ghost:disabled { background:#f4f5f7; color:#9aa2b1; border-color:var(--line); }
  .row { display:flex; gap:10px; align-items:center; }
  .row > input { flex:1; }
  .tabs { display:flex; gap:8px; margin-bottom:14px; overflow-x:auto; padding-bottom:2px; }
  .tabs button { flex:1 0 auto; background:#eef1f6; color:var(--sub); white-space:nowrap; padding:10px 16px; }
  .tabs button.on { background:var(--brand); color:#fff; }
  .hidden { display:none !important; }
  .msg { font-size:14px; margin-top:14px; min-height:22px; }
  .msg.bad { color:var(--bad); }
  .msg.good { color:var(--ok); }
  .muted { color:var(--sub); font-size:13px; }
  .num { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .kv { display:flex; justify-content:space-between; padding:8px 0; border-top:1px solid var(--line); font-size:14px; }
  .kv:first-child { border-top:0; }
  .pill { display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px; background:#eef1f6; color:var(--sub); }
  .pill.admin { background:var(--bad-soft); color:#c5221f; }
  .pill.vip { background:var(--warn-soft); color:var(--warn); }
  .pill.ok { background:var(--ok-soft); color:#0f7a45; }
  .pill.off { background:var(--bad-soft); color:#c5221f; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--sub); font-weight:500; }
  code { background:var(--soft); padding:2px 6px; border-radius:5px; font-size:13px; font-family:var(--mono); }

  /* ── 后台 ───────────────────────────────────────────────── */
  .apphead { display:flex; justify-content:space-between; align-items:center; gap:12px;
             margin-bottom:16px; flex-wrap:wrap; }
  .apphead .who { font-size:16px; font-weight:600; }
  button.sm { padding:6px 12px; font-size:13px; border-radius:7px; }
  button.danger { background:#fff; color:var(--bad); border:1px solid #f0c8c4; }
  button.danger:hover { background:var(--bad-soft); }
  button.danger[data-armed="1"] { background:var(--bad); color:#fff; border-color:var(--bad); }
  button.okbtn { background:#fff; color:var(--ok); border:1px solid #bfe3cd; }
  button.okbtn:hover { background:var(--ok-soft); }

  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:14px; }
  .stat { background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .stat .k { font-size:12px; color:var(--sub); }
  .stat .v { font-size:22px; font-weight:600; font-family:var(--mono); font-variant-numeric:tabular-nums;
             margin-top:4px; letter-spacing:-0.5px; }
  .stat .s { font-size:12px; color:var(--sub); margin-top:2px; }

  .chartbox { position:relative; margin-top:12px; }
  .chartbox svg { display:block; width:100%; height:auto; }
  .chart-empty { padding:56px 10px; text-align:center; color:var(--sub); font-size:13px;
                 background:repeating-linear-gradient(0deg,transparent,transparent 39px,#f0f2f6 39px,#f0f2f6 40px); border-radius:8px; }
  .tip { position:absolute; left:0; top:0; pointer-events:none; opacity:0; transition:opacity .12s;
         background:#16181d; color:#fff; font-size:12px; font-family:var(--mono); padding:4px 9px;
         border-radius:6px; white-space:nowrap; transform:translateX(-50%); z-index:3; }
  .chart-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap; }

  .rank { display:flex; flex-direction:column; gap:12px; margin-top:14px; }
  .rank .line { display:flex; align-items:baseline; gap:8px; font-size:13px; margin-bottom:5px; }
  .rank .rk { flex:0 0 18px; color:var(--sub); font-family:var(--mono); font-size:12px; }
  .rank .em { flex:1 1 auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rank .lab { flex:0 0 auto; color:var(--sub); font-size:12px; }
  .track { height:8px; background:var(--soft); border-radius:99px; overflow:hidden; }
  .track > i { display:block; height:100%; border-radius:99px; }

  .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .toolbar .search { flex:1 1 220px; }
  .toolbar select { padding:10px 12px; border:1px solid var(--line); border-radius:8px;
                    font-size:14px; background:#fff; color:var(--ink); }
  .tablewrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table.data { min-width:940px; }
  table.data th { white-space:nowrap; }
  table.data td { vertical-align:middle; }
  table.data input.sm, table.data select.sm { padding:5px 7px; font-size:13px; border-radius:6px;
                    border:1px solid var(--line); background:#fff; width:auto; }
  table.data input.q { width:76px; font-family:var(--mono); }
  table.data select.role { width:96px; }
  table.data td.ops { white-space:nowrap; }
  table.data td.ops button + button { margin-left:6px; }
  .rowmsg { font-size:12px; max-width:180px; }
  .rowmsg.good { color:var(--ok); }
  .rowmsg.bad { color:var(--bad); }
  .pager { display:flex; align-items:center; gap:12px; margin-top:14px; justify-content:flex-end; flex-wrap:wrap; }

  .formline { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  .formline > div { flex:1 1 130px; }
  .formline .formbtn { flex:0 0 auto; }
  .formline label { margin:0 0 6px; }
  .newcodes { margin-top:14px; padding:14px; border:1px dashed #c9d8ff; border-radius:10px; background:#f7faff; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
  .chip { font-family:var(--mono); font-size:13px; padding:7px 10px; border-radius:8px;
          background:var(--brand-soft); color:#1b4fd0; border:1px solid #d6e3ff; }

  .chans { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; margin-top:14px; }
  .chan { border:1px solid var(--line); border-radius:12px; padding:14px 16px; background:#fff; }
  .chan .head { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; }
  .chan .dot { font-size:13px; font-weight:600; white-space:nowrap; }
  .chan .dot.ok { color:var(--ok); }
  .chan .dot.bad { color:var(--bad); }
  .badge { display:inline-block; font-size:11px; padding:2px 7px; border-radius:99px;
           background:var(--soft); color:var(--sub); font-family:var(--mono); }
  .errbox { margin-top:10px; padding:8px 10px; border-radius:8px; background:var(--bad-soft);
            color:#a01b12; font-size:12px; word-break:break-word; }

  @media (max-width:600px) {
    .wrap { padding:22px 14px 48px; }
    .card { padding:16px; border-radius:10px; }
    .stat .v { font-size:19px; }
    .apphead { align-items:flex-start; }
  }
</style>
</head>
<body>
<div class="wrap" id="wrap">
  <div id="guest-head">
    <h1>Aurora 账户</h1>
    <p class="lead">ourmetaverse.cn 的统一账号。一个账号，通行文档翻译等各项服务。</p>
  </div>

  <!-- 未登录：登录 / 注册 / 忘记密码 -->
  <div id="guest">
    <div class="tabs">
      <button id="tab-login" class="on">登录</button>
      <button id="tab-register">注册</button>
    </div>

    <div class="card" id="view-login">
      <label>邮箱</label>
      <input id="li-email" type="email" autocomplete="username" placeholder="you@example.com">
      <label>密码</label>
      <input id="li-pass" type="password" autocomplete="current-password" placeholder="至少 8 位">
      <div style="margin-top:18px" class="row">
        <button id="btn-login">登录</button>
        <button id="btn-forgot" class="ghost">忘记密码</button>
      </div>
      <p class="msg" id="li-msg"></p>
    </div>

    <div class="card hidden" id="view-register">
      <label>邮箱</label>
      <input id="rg-email" type="email" autocomplete="username" placeholder="you@example.com">
      <label>验证码（6 位数字）</label>
      <div class="row">
        <input id="rg-code" inputmode="numeric" maxlength="6" placeholder="6 位数字">
        <button id="btn-sendcode" class="ghost" style="white-space:nowrap">发送验证码</button>
      </div>
      <label>设置密码</label>
      <input id="rg-pass" type="password" autocomplete="new-password" placeholder="至少 8 位，建议 12 位以上">
      <label>邀请码（选填）</label>
      <input id="rg-invite" placeholder="有邀请码 → 自动成为 VIP（每月 50 页）">
      <div style="margin-top:18px"><button id="btn-register">注册并登录</button></div>
      <p class="msg" id="rg-msg"></p>
      <p class="muted" style="margin-top:12px">第一个注册的账号自动成为管理员。注册即表示同意：本站仅用于个人与朋友之间的文档翻译，请勿上传违法内容。</p>
    </div>

    <div class="card hidden" id="view-forgot">
      <label>邮箱</label>
      <input id="fg-email" type="email" placeholder="you@example.com">
      <label>验证码（6 位数字）</label>
      <div class="row">
        <input id="fg-code" inputmode="numeric" maxlength="6" placeholder="6 位数字">
        <button id="btn-sendcode2" class="ghost" style="white-space:nowrap">发送验证码</button>
      </div>
      <label>新密码</label>
      <input id="fg-pass" type="password" autocomplete="new-password" placeholder="至少 8 位">
      <div style="margin-top:18px" class="row">
        <button id="btn-reset">重置密码</button>
        <button id="btn-back" class="ghost">返回登录</button>
      </div>
      <p class="msg" id="fg-msg"></p>
    </div>
  </div>

  <!-- 已登录 -->
  <div id="app" class="hidden">
    <div class="apphead">
      <div>
        <div class="who" id="app-title">Aurora 账户</div>
        <div class="muted" id="me-email"></div>
      </div>
      <div class="row">
        <span id="me-role"></span>
        <button id="btn-logout" class="ghost sm">退出登录</button>
      </div>
    </div>

    <!-- 管理员顶部 tab（普通用户看不到） -->
    <div class="tabs hidden" id="app-tabs">
      <button id="tab-me" data-tab="me" class="on">个人中心</button>
      <button id="tab-overview" data-tab="overview">总览</button>
      <button id="tab-users" data-tab="users">用户</button>
      <button id="tab-invites" data-tab="invites">邀请码</button>
      <button id="tab-channels" data-tab="channels">通道</button>
    </div>

    <!-- ── 个人中心 ── -->
    <section id="pane-me">
      <div class="card">
        <div style="font-size:16px;font-weight:600" id="me-email2"></div>
        <div style="margin-top:16px">
          <div class="kv"><span>本月可用页数</span><b class="num" id="me-quota">—</b></div>
          <div class="kv"><span>本月已用</span><b class="num" id="me-used">—</b></div>
          <div class="kv"><span>累计翻译</span><b class="num" id="me-total">—</b></div>
          <div class="kv"><span>注册时间</span><b class="num" id="me-created">—</b></div>
        </div>
      </div>

      <div class="card">
        <div style="font-weight:600;margin-bottom:6px">修改密码</div>
        <label>原密码</label>
        <input id="cp-old" type="password" autocomplete="current-password">
        <label>新密码</label>
        <input id="cp-new" type="password" autocomplete="new-password" placeholder="至少 8 位">
        <div style="margin-top:16px"><button id="btn-changepass">修改</button></div>
        <p class="msg" id="cp-msg"></p>
      </div>

      <div class="card" id="usage-card">
        <div style="font-weight:600;margin-bottom:6px">最近使用记录</div>
        <div class="tablewrap">
          <table>
            <thead><tr><th>时间</th><th>类型</th><th>页数</th></tr></thead>
            <tbody id="usage-body"><tr><td colspan="3" class="muted">暂无记录</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ── 总览 ── -->
    <section id="pane-overview" class="hidden">
      <div class="stats">
        <div class="stat"><div class="k">总用户数</div><div class="v" id="ov-users">—</div><div class="s" id="ov-sub-users">全部注册账号</div></div>
        <div class="stat"><div class="k">近 7 天新增</div><div class="v" id="ov-new7">—</div><div class="s">按注册时间</div></div>
        <div class="stat"><div class="k">本月翻译页数</div><div class="v" id="ov-pages">—</div><div class="s" id="ov-sub-pages">—</div></div>
        <div class="stat"><div class="k">本月任务数</div><div class="v" id="ov-jobs">—</div><div class="s">usage 流水条数</div></div>
        <div class="stat"><div class="k">邀请码累计使用</div><div class="v" id="ov-invites">—</div><div class="s" id="ov-sub-invites">—</div></div>
      </div>

      <div class="card">
        <div class="chart-head">
          <div>
            <b>近 14 天每日翻译页数</b>
            <div class="muted" style="margin-top:2px">按北京时间自然日统计 · 鼠标悬停柱子看当天数值</div>
          </div>
          <div class="muted num" id="ov-range"></div>
        </div>
        <div class="chartbox" id="chartbox">
          <div id="chart"><div class="chart-empty">加载中…</div></div>
          <div class="tip" id="tip"></div>
        </div>
      </div>

      <div class="card">
        <b>用户额度排行 · 前 5</b>
        <div class="muted" style="margin-top:2px">按本月已用页数排序</div>
        <div class="rank" id="rank"><p class="muted">加载中…</p></div>
      </div>
    </section>

    <!-- ── 用户管理 ── -->
    <section id="pane-users" class="hidden">
      <div class="card">
        <div class="toolbar">
          <input id="u-q" class="search" type="search" placeholder="搜索邮箱（模糊匹配）">
          <select id="u-sort">
            <option value="created_at">按注册时间</option>
            <option value="used_pages">按已用页数</option>
            <option value="quota_pages">按额度</option>
            <option value="last_login_at">按最后登录</option>
          </select>
          <button id="u-order" class="ghost sm" type="button">↓ 降序</button>
        </div>
        <div class="tablewrap" style="margin-top:12px">
          <table class="data">
            <thead>
              <tr><th>邮箱</th><th>角色</th><th>状态</th><th>精修</th><th>额度</th><th>已用</th>
                  <th>注册时间</th><th>最后登录</th><th>操作</th><th>提示</th></tr>
            </thead>
            <tbody id="user-body"><tr><td colspan="10" class="muted">加载中…</td></tr></tbody>
          </table>
        </div>
        <div class="pager">
          <button id="u-prev" class="ghost sm" type="button">上一页</button>
          <span class="muted num" id="u-pageinfo">—</span>
          <button id="u-next" class="ghost sm" type="button">下一页</button>
        </div>
        <p class="muted" style="margin-top:12px">
          改角色会自动套用该角色默认额度（普通 30 / VIP 50 / 管理员 200）；额度填 <b>0</b> 表示不限。
          封号会把该用户所有会话立即踢下线；当前登录的管理员不能封自己。
        </p>
      </div>
    </section>

    <!-- ── 邀请码 ── -->
    <section id="pane-invites" class="hidden">
      <div class="card">
        <div style="font-weight:600">生成邀请码</div>
        <p class="muted" style="margin-top:6px">朋友注册时填上邀请码，自动成为 <b>VIP（每月 50 页）</b>。</p>
        <div class="formline" style="margin-top:14px">
          <div><label>数量（1~20）</label><input id="iv-count" type="number" min="1" max="20" value="3"></div>
          <div><label>每个可用次数（1~100）</label><input id="iv-uses" type="number" min="1" max="100" value="1"></div>
          <div><label>有效天数（1~3650）</label><input id="iv-days" type="number" min="1" max="3650" value="30"></div>
          <div class="formbtn"><button id="btn-newinvite" type="button">生成</button></div>
        </div>
        <p class="msg" id="inv-msg"></p>
        <div id="new-codes" class="newcodes hidden"></div>
      </div>

      <div class="card">
        <div style="font-weight:600;margin-bottom:10px">邀请码列表</div>
        <div class="tablewrap">
          <table>
            <thead><tr><th>邀请码</th><th>已用 / 上限</th><th>有效期</th><th>状态</th><th></th></tr></thead>
            <tbody id="invite-body"><tr><td colspan="5" class="muted">加载中…</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ── 通道 ── -->
    <section id="pane-channels" class="hidden">
      <div class="card">
        <div class="row" style="justify-content:space-between;flex-wrap:wrap">
          <div>
            <b>通道状态与余额</b>
            <div class="muted" style="margin-top:2px">对每个通道实发一个最小请求测连通性与延迟，每通道 3 秒超时</div>
          </div>
          <button id="btn-refresh-ch" class="ghost sm" type="button">刷新</button>
        </div>
        <p class="msg" id="ch-note">点「刷新」开始探测</p>
        <div class="chans" id="ch-list"></div>
      </div>
    </section>
  </div>
</div>

<script>
var $ = function (id) { return document.getElementById(id); };
function show(id, on) { var el = $(id); if (el) el.classList.toggle('hidden', !on); }
function setMsg(id, text, kind) { var el = $(id); if (!el) return; el.textContent = text || ''; el.className = 'msg' + (kind ? ' ' + kind : ''); }
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function req(path, opts) {
  return fetch(path, opts).then(function (r) {
    return r.text().then(function (t) {
      var d;
      try { d = JSON.parse(t); } catch (e) { d = { ok: false, error: '服务端返回了非 JSON 内容（HTTP ' + r.status + '）' }; }
      d._status = r.status;
      return d;
    });
  });
}
function post(path, body) {
  return req(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {})
  });
}
function get(path) { return req(path, { credentials: 'same-origin' }); }
function busy(btn, on, label) {
  if (!btn) return;
  btn.disabled = on;
  if (on) { btn.dataset._t = btn.textContent; btn.textContent = label || '请稍候…'; }
  else if (btn.dataset._t) { btn.textContent = btn.dataset._t; }
}

/* ── 人性化格式 ── */
function pad2(n) { return (n < 10 ? '0' : '') + n; }
function fmtDate(sec) {
  if (!sec) return '—';
  var d = new Date(sec * 1000);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}
function fmtDateTime(sec) {
  if (!sec) return '—';
  var d = new Date(sec * 1000);
  return fmtDate(sec) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}
function fmtAgo(sec) {
  if (!sec) return '从未登录';
  var diff = Math.floor(Date.now() / 1000) - sec;
  if (diff < 0) return fmtDateTime(sec);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  if (diff < 7 * 86400) return Math.floor(diff / 86400) + ' 天前';
  return fmtDate(sec);
}
function fmtNum(n) {
  var s = String(n == null ? 0 : n);
  var neg = s.charAt(0) === '-';
  if (neg) s = s.slice(1);
  var out = '';
  var c = 0;
  for (var i = s.length - 1; i >= 0; i -= 1) {
    out = s.charAt(i) + out;
    c += 1;
    if (c % 3 === 0 && i > 0) out = ',' + out;
  }
  return (neg ? '-' : '') + out;
}
function roleName(r) { return r === 'admin' ? '管理员' : (r === 'vip' ? 'VIP' : '普通用户'); }
function round1(v) { var r = Math.round(v * 10) / 10; return String(r === Math.trunc(r) ? Math.trunc(r) : r); }
function copyText(text, btn, restore) {
  var done = function () {
    if (!btn || !btn.isConnected) return;
    var old = restore || btn.textContent;
    btn.textContent = '已复制';
    setTimeout(function () { if (btn.isConnected) btn.textContent = old; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
  } else { fallbackCopy(text); done(); }
}
function fallbackCopy(text) {
  try {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  } catch (e) { /* 复制不了就算了，码还在页面上 */ }
}

/* ── 标签切换 ── */
function tab(which) {
  var isLogin = which === 'login';
  $('tab-login').className = isLogin ? 'on' : '';
  $('tab-register').className = isLogin ? '' : 'on';
  show('view-login', isLogin); show('view-register', !isLogin); show('view-forgot', false);
}
$('tab-login').onclick = function () { tab('login'); };
$('tab-register').onclick = function () { tab('register'); };
$('btn-forgot').onclick = function () { show('view-login', false); show('view-register', false); show('view-forgot', true); };
$('btn-back').onclick = function () { tab('login'); };

/* ── 登录 ── */
$('btn-login').onclick = function () {
  var btn = this;
  setMsg('li-msg', '');
  busy(btn, true, '登录中…');
  post('/api/login', { email: $('li-email').value, password: $('li-pass').value })
    .then(function (d) {
      busy(btn, false);
      if (!d.ok) { setMsg('li-msg', d.error || '登录失败', 'bad'); return; }
      $('li-pass').value = '';
      loadMe();
    }).catch(function () { busy(btn, false); setMsg('li-msg', '网络错误，请重试', 'bad'); });
};

/* ── 发验证码 ── */
function sendCode(emailId, purpose, btn, msgId) {
  var email = $(emailId).value;
  setMsg(msgId, '');
  busy(btn, true, '发送中…');
  post('/api/send-code', { email: email, purpose: purpose }).then(function (d) {
    busy(btn, false);
    if (!d.ok) { setMsg(msgId, d.error || '发送失败', 'bad'); return; }
    setMsg(msgId, '验证码已发送，请查收邮箱（10 分钟内有效）' +
      (d.dev_code ? '【本地调试码：' + d.dev_code + '】' : ''), 'good');
    var n = 60;
    btn.disabled = true;
    var t = setInterval(function () {
      n -= 1; btn.textContent = n + ' 秒后可重发';
      if (n <= 0) { clearInterval(t); btn.disabled = false; btn.textContent = '发送验证码'; }
    }, 1000);
  }).catch(function () { busy(btn, false); setMsg(msgId, '网络错误', 'bad'); });
}
$('btn-sendcode').onclick = function () { sendCode('rg-email', 'register', this, 'rg-msg'); };
$('btn-sendcode2').onclick = function () { sendCode('fg-email', 'reset', this, 'fg-msg'); };

/* ── 注册 ── */
$('btn-register').onclick = function () {
  var btn = this;
  setMsg('rg-msg', '');
  busy(btn, true, '注册中…');
  post('/api/register', {
    email: $('rg-email').value, code: $('rg-code').value,
    password: $('rg-pass').value, invite: $('rg-invite').value
  }).then(function (d) {
    busy(btn, false);
    if (!d.ok) { setMsg('rg-msg', d.error || '注册失败', 'bad'); return; }
    $('rg-pass').value = ''; $('rg-code').value = ''; $('rg-invite').value = '';
    if (d.message) setMsg('rg-msg', d.message, 'good');
    loadMe();
  }).catch(function () { busy(btn, false); setMsg('rg-msg', '网络错误', 'bad'); });
};

/* ── 重置密码 ── */
$('btn-reset').onclick = function () {
  var btn = this;
  setMsg('fg-msg', '');
  busy(btn, true, '提交中…');
  post('/api/reset-password', {
    email: $('fg-email').value, code: $('fg-code').value, password: $('fg-pass').value
  }).then(function (d) {
    busy(btn, false);
    if (!d.ok) { setMsg('fg-msg', d.error || '重置失败', 'bad'); return; }
    setMsg('fg-msg', d.message || '已重置，请登录', 'good');
    setTimeout(function () { tab('login'); $('li-email').value = $('fg-email').value; }, 1200);
  }).catch(function () { busy(btn, false); setMsg('fg-msg', '网络错误', 'bad'); });
};

/* ── 退出 ── */
$('btn-logout').onclick = function () {
  post('/api/logout', {}).then(function () { state.tab = 'me'; loadMe(); });
};

/* ── 改密码 ── */
$('btn-changepass').onclick = function () {
  var btn = this;
  setMsg('cp-msg', '');
  busy(btn, true, '修改中…');
  post('/api/change-password', { old_password: $('cp-old').value, new_password: $('cp-new').value })
    .then(function (d) {
      busy(btn, false);
      if (!d.ok) { setMsg('cp-msg', d.error || '修改失败', 'bad'); return; }
      setMsg('cp-msg', '已修改，请用新密码重新登录', 'good');
      $('cp-old').value = ''; $('cp-new').value = '';
      setTimeout(loadMe, 1200);
    }).catch(function () { busy(btn, false); setMsg('cp-msg', '网络错误', 'bad'); });
};

/* ── 全局状态 ── */
var state = {
  me: null,
  tab: 'me',
  users: { users: [], total: 0, page: 1, pages: 1, page_size: 20 },
  order: 'desc',
  daily: null,
  channelsLoaded: false
};

function showTab(name) {
  if (name !== 'me' && !(state.me && state.me.role === 'admin')) name = 'me';
  state.tab = name;
  var panes = ['me', 'overview', 'users', 'invites', 'channels'];
  for (var i = 0; i < panes.length; i += 1) {
    show('pane-' + panes[i], panes[i] === name);
    var b = $('tab-' + panes[i]);
    if (b) b.className = panes[i] === name ? 'on' : '';
  }
  if (name === 'overview') loadStats();
  if (name === 'users') loadUsers(1);
  if (name === 'invites') loadInvites();
  if (name === 'channels' && !state.channelsLoaded) loadChannels();
}
$('app-tabs').addEventListener('click', function (e) {
  var b = e.target.closest ? e.target.closest('button[data-tab]') : null;
  if (b) showTab(b.getAttribute('data-tab'));
});

/* ── 个人中心渲染 ── */
function loadMe() {
  get('/api/me').then(function (d) {
    if (!d.ok) {
      state.me = null;
      show('guest', true); show('guest-head', true); show('app', false);
      $('wrap').className = 'wrap';
      if (d._status === 401) setMsg('li-msg', '');
      return;
    }
    state.me = d.user;
    show('guest', false); show('guest-head', false); show('app', true);
    var role = d.user.role;
    var isAdmin = role === 'admin';
    $('wrap').className = 'wrap' + (isAdmin ? ' wide' : '');
    $('app-title').textContent = isAdmin ? 'Aurora 管理后台' : 'Aurora 账户';
    $('me-email').textContent = d.user.email;
    $('me-email2').textContent = d.user.email;
    $('me-role').innerHTML = '<span class="pill ' + role + '">' + roleName(role) + '</span>';
    $('me-quota').textContent = d.user.quota_pages === 0 ? '不限' : fmtNum(d.user.quota_pages) + ' 页';
    $('me-used').textContent = fmtNum(d.user.used_pages) + ' 页';
    $('me-total').textContent = fmtNum(d.usage_total.jobs || 0) + ' 个文件 · ' +
      fmtNum(d.usage_total.pages || 0) + ' 页';
    $('me-created').textContent = fmtDate(d.user.created_at);
    show('app-tabs', isAdmin);
    showTab(isAdmin ? state.tab : 'me');
    loadUsage();
  }).catch(function () {});
}

function loadUsage() {
  get('/api/usage').then(function (d) {
    if (!d.ok) return;
    var body = $('usage-body');
    body.innerHTML = '';
    if (!d.items.length) {
      body.innerHTML = '<tr><td colspan="3" class="muted">暂无记录（翻译功能接入后，这里会显示每个文件用了多少页）</td></tr>';
      return;
    }
    d.items.forEach(function (it) {
      var tr = document.createElement('tr');
      tr.innerHTML = '<td class="num">' + fmtDate(it.created_at) + '</td><td>' + esc(it.note || it.kind) +
        '</td><td class="num">' + fmtNum(it.pages) + '</td>';
      body.appendChild(tr);
    });
  }).catch(function () {});
}

/* ── 总览 ── */
function niceMax(v) {
  if (v <= 4) return 4;
  var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
  var cands = [1, 2, 2.5, 5, 10];
  for (var i = 0; i < cands.length; i += 1) {
    if (cands[i] * p >= v) return cands[i] * p;
  }
  return 10 * p;
}

function drawChart(daily) {
  var host = $('chart');
  if (!daily || !daily.length) {
    host.innerHTML = '<div class="chart-empty">暂无数据 · 还没有可统计的翻译记录</div>';
    return;
  }
  var max = 0;
  daily.forEach(function (d) { if (d.pages > max) max = d.pages; });
  if (max <= 0) {
    host.innerHTML = '<div class="chart-empty">暂无数据 · 最近 14 天还没有翻译记录</div>';
    return;
  }

  var W = Math.max(300, Math.round(host.clientWidth || 700));
  var H = W < 480 ? 200 : 250;
  var L = W < 480 ? 34 : 48;
  var R = 8;
  var T = 14;
  var B = 34;
  var pw = W - L - R;
  var ph = H - T - B;
  var top = niceMax(max);
  var n = daily.length;
  var slot = pw / n;
  var bw = Math.max(5, Math.min(slot * 0.56, 34));
  var fs = W < 480 ? 9 : 11;

  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '" role="img" aria-label="近 14 天每日翻译页数柱状图">';
  for (var g = 0; g <= 4; g += 1) {
    var y = T + ph - (ph * g / 4);
    s += '<line x1="' + L + '" y1="' + y + '" x2="' + (L + pw) + '" y2="' + y + '" stroke="' +
      (g === 0 ? '#c9ced8' : '#eef1f6') + '" stroke-width="1"/>';
    s += '<text x="' + (L - 7) + '" y="' + (y + 4) + '" text-anchor="end" font-size="' + fs +
      '" fill="#8b93a1">' + round1(top * g / 4) + '</text>';
  }
  var every = slot < 30 ? 2 : 1;
  daily.forEach(function (d, i) {
    var h = (d.pages / top) * ph;
    var zero = d.pages <= 0;
    if (h < 3) h = 3;
    var x = L + slot * i + (slot - bw) / 2;
    var y = T + ph - h;
    var cx = L + slot * i + slot / 2;
    s += '<rect class="bar-hit" x="' + (L + slot * i) + '" y="' + T + '" width="' + slot + '" height="' + ph +
      '" data-v="' + d.pages + '" data-d="' + d.day + '" data-j="' + d.jobs + '"/>';
    s += '<rect x="' + x + '" y="' + y + '" width="' + bw + '" height="' + h + '" rx="3" fill="' +
      (zero ? '#e5e7eb' : '#2f6df6') + '" pointer-events="none"/>';
    if (i % every === 0) {
      s += '<text x="' + cx + '" y="' + (T + ph + 15) + '" text-anchor="middle" font-size="' + fs +
        '" fill="#8b93a1">' + d.day.slice(5) + '</text>';
    }
  });
  s += '</svg>';
  host.innerHTML = s;
}

(function chartHover() {
  var box = $('chartbox');
  var tip = $('tip');
  function move(e) {
    var t = e.target;
    var v = t && t.getAttribute ? t.getAttribute('data-v') : null;
    if (v == null) { tip.style.opacity = '0'; return; }
    tip.textContent = t.getAttribute('data-d') + ' · ' + v + ' 页 · ' + t.getAttribute('data-j') + ' 个任务';
    var r = box.getBoundingClientRect();
    var x = e.clientX - r.left;
    var y = e.clientY - r.top;
    var half = 70;
    tip.style.left = Math.min(Math.max(half, x), Math.max(half, r.width - half)) + 'px';
    tip.style.top = Math.max(0, y - 36) + 'px';
    tip.style.opacity = '1';
  }
  box.addEventListener('mousemove', move);
  box.addEventListener('click', move);
  box.addEventListener('mouseleave', function () { tip.style.opacity = '0'; });
})();

var rsz = null;
window.addEventListener('resize', function () {
  if (state.tab !== 'overview' || !state.daily) return;
  if (rsz) clearTimeout(rsz);
  rsz = setTimeout(function () { drawChart(state.daily); }, 200);
});

function drawRank(list) {
  var host = $('rank');
  if (!list || !list.length) { host.innerHTML = '<p class="muted">暂无数据 · 还没有用户用量</p>'; return; }
  var html = '';
  list.forEach(function (u, i) {
    var pct = u.percent;
    var w = pct == null ? 100 : Math.max(2, Math.min(100, pct));
    var color = pct == null ? '#8b93a1' : (pct >= 90 ? '#d93025' : (pct >= 60 ? '#e0a33c' : '#2f6df6'));
    html += '<div>'
      + '<div class="line"><span class="rk">' + (i + 1) + '</span>'
      + '<span class="em" title="' + esc(u.email) + '">' + esc(u.email) + '</span>'
      + '<span class="lab num">' + fmtNum(u.used_pages) + ' / ' +
      (u.quota_pages === 0 ? '不限' : fmtNum(u.quota_pages)) +
      (pct == null ? '' : ' · ' + pct + '%') + '</span></div>'
      + '<div class="track"><i style="width:' + w + '%;background:' + color + '"></i></div>'
      + '</div>';
  });
  host.innerHTML = html;
}

function loadStats() {
  ['ov-users', 'ov-new7', 'ov-pages', 'ov-jobs', 'ov-invites'].forEach(function (id) { $(id).textContent = '—'; });
  $('chart').innerHTML = '<div class="chart-empty">加载中…</div>';
  $('rank').innerHTML = '<p class="muted">加载中…</p>';
  get('/api/admin/stats').then(function (d) {
    if (!d.ok) {
      $('chart').innerHTML = '<div class="chart-empty">' + esc(d.error || '加载失败') + '</div>';
      $('rank').innerHTML = '<p class="muted">' + esc(d.error || '加载失败') + '</p>';
      state.daily = null;
      return;
    }
    $('ov-users').textContent = fmtNum(d.users_total);
    $('ov-new7').textContent = fmtNum(d.users_new_7d);
    $('ov-pages').textContent = fmtNum(d.month_pages);
    $('ov-jobs').textContent = fmtNum(d.month_jobs);
    $('ov-invites').textContent = fmtNum(d.invite_uses);
    $('ov-sub-pages').textContent = '累计 ' + fmtNum(d.total_pages) + ' 页 / ' + fmtNum(d.total_jobs) + ' 个任务';
    $('ov-sub-invites').textContent = '共发出 ' + fmtNum(d.invite_total) + ' 个码';
    state.daily = d.daily || [];
    drawChart(state.daily);
    drawRank(d.top_users || []);
    $('ov-range').textContent = state.daily.length
      ? (state.daily[0].day + ' ~ ' + state.daily[state.daily.length - 1].day) : '';
  }).catch(function () {
    $('chart').innerHTML = '<div class="chart-empty">网络错误</div>';
    $('rank').innerHTML = '<p class="muted">网络错误</p>';
  });
}

/* ── 用户管理 ── */
function roleOpts(cur) {
  var opts = [['user', '普通用户'], ['vip', 'VIP'], ['admin', '管理员']];
  var s = '';
  opts.forEach(function (o) {
    s += '<option value="' + o[0] + '"' + (o[0] === cur ? ' selected' : '') + '>' + o[1] + '</option>';
  });
  return s;
}
function statusPill(st) {
  return st === 'banned' ? '<span class="pill off">已停用</span>' : '<span class="pill ok">正常</span>';
}
function userRowHtml(u, i, isMe) {
  return '<tr data-id="' + u.id + '" data-i="' + i + '">'
    + '<td>' + esc(u.email) + (isMe ? ' <span class="pill">你自己</span>' : '') + '</td>'
    + '<td><select class="sm role">' + roleOpts(u.role) + '</select></td>'
    + '<td>' + statusPill(u.status) + '</td>'
    + '<td><input class="sm refine" type="checkbox"' + (Number(u.can_refine) === 1 ? ' checked' : '') + '></td>'
    + '<td><input class="sm q" type="number" min="0" step="10" value="' + (Number(u.quota_pages) || 0) + '"></td>'
    + '<td class="num">' + fmtNum(u.used_pages) + '</td>'
    + '<td class="num">' + fmtDate(u.created_at) + '</td>'
    + '<td class="num">' + fmtAgo(u.last_login_at) + '</td>'
    + '<td class="ops">'
    + '<button class="sm save" type="button">保存额度</button>'
    + '<button class="sm ' + (u.status === 'banned' ? 'okbtn unban' : 'danger ban') + '" type="button"' +
      (isMe ? ' disabled title="不能封禁自己"' : '') + '>' + (u.status === 'banned' ? '解封' : '封号') + '</button>'
    + '<button class="sm ghost reset" type="button">重置用量</button>'
    + '</td>'
    + '<td class="rowmsg"></td>'
    + '</tr>';
}
function rowMsg(tr, text, kind) {
  var td = tr.querySelector('.rowmsg');
  if (!td) return;
  td.textContent = text || '';
  td.className = 'rowmsg' + (kind ? ' ' + kind : '');
  if (tr._t) clearTimeout(tr._t);
  if (text) tr._t = setTimeout(function () { td.textContent = ''; td.className = 'rowmsg'; }, 4000);
}
function renderUsers() {
  var body = $('user-body');
  var d = state.users;
  var list = d.users || [];
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="10" class="muted">' +
      (d.q ? '没有匹配「' + esc(d.q) + '」的用户' : '还没有用户') + '</td></tr>';
  } else {
    var html = '';
    for (var i = 0; i < list.length; i += 1) {
      html += userRowHtml(list[i], i, state.me && list[i].id === state.me.id);
    }
    body.innerHTML = html;
  }
  $('u-pageinfo').textContent = '第 ' + d.page + ' / ' + d.pages + ' 页 · 共 ' + fmtNum(d.total) + ' 人';
  $('u-prev').disabled = d.page <= 1;
  $('u-next').disabled = d.page >= d.pages;
}
function loadUsers(page) {
  if (page) state.users.page = page;
  var p = state.users.page || 1;
  var qs = '?q=' + encodeURIComponent($('u-q').value.trim()) +
    '&sort=' + encodeURIComponent($('u-sort').value) +
    '&order=' + state.order + '&page=' + p + '&page_size=20';
  var body = $('user-body');
  body.innerHTML = '<tr><td colspan="10" class="muted">加载中…</td></tr>';
  get('/api/admin/users' + qs).then(function (d) {
    if (!d.ok) {
      body.innerHTML = '<tr><td colspan="10" class="muted">' + esc(d.error || '加载失败') + '</td></tr>';
      $('u-pageinfo').textContent = '—';
      return;
    }
    state.users = d;
    renderUsers();
  }).catch(function () {
    body.innerHTML = '<tr><td colspan="10" class="muted">网络错误</td></tr>';
  });
}
function patchRow(tr, u) {
  var i = Number(tr.getAttribute('data-i'));
  state.users.users[i] = u;
  tr.outerHTML = userRowHtml(u, i, state.me && u.id === state.me.id);
  return $('user-body').querySelector('tr[data-i="' + i + '"]');
}
function updateUser(tr, u, payload, okText) {
  rowMsg(tr, '保存中…', '');
  post('/api/admin/update', Object.assign({ user_id: u.id }, payload)).then(function (d) {
    if (!d.ok) {
      // 保存失败就把下拉框拨回原值，避免界面显示一个其实没生效的角色
      if (payload.role) {
        var sel = tr.querySelector('select.role');
        if (sel) sel.value = u.role;
      }
      // 勾选框同理：失败就拨回库里的值，免得「界面开着、其实没开」
      if (payload.can_refine !== undefined) {
        var box = tr.querySelector('input.refine');
        if (box) box.checked = Number(u.can_refine) === 1;
      }
      rowMsg(tr, d.error || '保存失败', 'bad');
      return;
    }
    var nt = d.user ? patchRow(tr, d.user) : tr;
    rowMsg(nt, okText || '已保存', 'good');
    if (state.me && u.id === state.me.id) setTimeout(loadMe, 900);
  }).catch(function () { rowMsg(tr, '网络错误', 'bad'); });
}
function saveQuota(tr, u) {
  var v = Number(tr.querySelector('input.q').value);
  if (!isFinite(v) || v < 0) { rowMsg(tr, '额度要填 0 或正整数', 'bad'); return; }
  v = Math.round(v);
  updateUser(tr, u, { quota_pages: v }, '额度已保存：' + (v === 0 ? '不限' : fmtNum(v) + ' 页'));
}
function setStatus(tr, u, status) {
  if (status === 'banned' && state.me && u.id === state.me.id) { rowMsg(tr, '不能封禁自己', 'bad'); return; }
  var btn = tr.querySelector('button.ban');
  if (status === 'banned' && btn && btn.getAttribute('data-armed') !== '1') {
    btn.setAttribute('data-armed', '1');
    btn.textContent = '再点一次确认';
    rowMsg(tr, '封号会立刻踢掉该用户所有会话', 'bad');
    setTimeout(function () {
      if (btn.isConnected) { btn.removeAttribute('data-armed'); btn.textContent = '封号'; }
    }, 4000);
    return;
  }
  updateUser(tr, u, { status: status }, status === 'banned' ? '已封禁，会话已失效' : '已解封');
}
$('user-body').addEventListener('click', function (e) {
  var btn = e.target.closest ? e.target.closest('button') : null;
  if (!btn || btn.disabled) return;
  var tr = btn.closest('tr');
  var u = tr && state.users.users[Number(tr.getAttribute('data-i'))];
  if (!u) return;
  if (btn.classList.contains('save')) saveQuota(tr, u);
  else if (btn.classList.contains('ban')) setStatus(tr, u, 'banned');
  else if (btn.classList.contains('unban')) setStatus(tr, u, 'active');
  else if (btn.classList.contains('reset')) updateUser(tr, u, { reset_used: 1 }, '本月用量已重置为 0');
});
$('user-body').addEventListener('change', function (e) {
  var sel = e.target;
  if (!sel.classList) return;
  var tr = sel.closest('tr');
  var u = tr && state.users.users[Number(tr.getAttribute('data-i'))];
  if (!u) return;
  if (sel.classList.contains('role')) {
    updateUser(tr, u, { role: sel.value }, '角色已改为' + roleName(sel.value) + '，额度已套用默认值');
    return;
  }
  // 精修能力位：改角色**不会**自动带上它（见 cf/account/worker.js 的注释），这里单独开/关
  if (sel.classList.contains('refine')) {
    var on = sel.checked;
    // 回滚点：接口失败时要把勾拨回去，否则界面显示的和库里存的不一致
    updateUser(tr, u, { can_refine: on ? 1 : 0 }, on ? '已允许精修' : '已关闭精修');
  }
});

var qTimer = null;
$('u-q').addEventListener('input', function () {
  if (qTimer) clearTimeout(qTimer);
  qTimer = setTimeout(function () { loadUsers(1); }, 400);
});
$('u-q').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') { if (qTimer) clearTimeout(qTimer); loadUsers(1); }
});
$('u-sort').addEventListener('change', function () { loadUsers(1); });
$('u-order').onclick = function () {
  state.order = state.order === 'desc' ? 'asc' : 'desc';
  this.textContent = state.order === 'desc' ? '↓ 降序' : '↑ 升序';
  loadUsers(1);
};
$('u-prev').onclick = function () { if (state.users.page > 1) loadUsers(state.users.page - 1); };
$('u-next').onclick = function () { if (state.users.page < state.users.pages) loadUsers(state.users.page + 1); };

/* ── 邀请码（管理员）── */
$('btn-newinvite').onclick = function () {
  var btn = this;
  var count = Number($('iv-count').value);
  var uses = Number($('iv-uses').value);
  var days = Number($('iv-days').value);
  setMsg('inv-msg', '');
  if (!isFinite(count) || count < 1 || count > 20) { setMsg('inv-msg', '数量请填 1~20', 'bad'); return; }
  if (!isFinite(uses) || uses < 1 || uses > 100) { setMsg('inv-msg', '每个可用次数请填 1~100', 'bad'); return; }
  if (!isFinite(days) || days < 1 || days > 3650) { setMsg('inv-msg', '有效天数请填 1~3650', 'bad'); return; }
  busy(btn, true, '生成中…');
  post('/api/admin/invite', { count: count, max_uses: uses, days: days }).then(function (d) {
    busy(btn, false);
    if (!d.ok) { setMsg('inv-msg', d.error || '生成失败', 'bad'); return; }
    setMsg('inv-msg', '已生成 ' + d.codes.length + ' 个邀请码（每个可用 ' + d.max_uses + ' 次，' + d.days + ' 天有效）', 'good');
    renderNewCodes(d.codes);
    loadInvites();
  }).catch(function () { busy(btn, false); setMsg('inv-msg', '网络错误', 'bad'); });
};

function renderNewCodes(codes) {
  var box = $('new-codes');
  if (!codes || !codes.length) { box.className = 'newcodes hidden'; box.innerHTML = ''; return; }
  var html = '<div class="row" style="justify-content:space-between">'
    + '<b style="font-size:13px">刚生成的邀请码（点一下复制）</b>'
    + '<button class="ghost sm" id="iv-copyall" type="button">复制全部</button></div><div class="chips">';
  codes.forEach(function (c) { html += '<button class="chip" data-code="' + esc(c) + '" type="button">' + esc(c) + '</button>'; });
  html += '</div>';
  box.className = 'newcodes';
  box.innerHTML = html;
  var all = $('iv-copyall');
  if (all) all.onclick = function () { copyText(codes.join(String.fromCharCode(10)), this, '复制全部'); };
  box.querySelectorAll('.chip').forEach(function (el) {
    el.onclick = function () {
      copyText(el.getAttribute('data-code'), el, el.getAttribute('data-code'));
    };
  });
}

function inviteState(it, now) {
  if (it.expires_at && it.expires_at < now) return { cls: 'off', text: '已过期' };
  if (it.used_count >= it.max_uses) return { cls: 'off', text: '已用完' };
  return { cls: 'ok', text: '可用' };
}
function inviteExpiry(it, now) {
  if (!it.expires_at) return '长期有效';
  if (it.expires_at < now) return fmtDate(it.expires_at) + '（已过期）';
  return fmtDate(it.expires_at) + '（剩 ' + Math.ceil((it.expires_at - now) / 86400) + ' 天）';
}
function loadInvites() {
  get('/api/admin/invites').then(function (d) {
    var body = $('invite-body');
    if (!d.ok) {
      body.innerHTML = '<tr><td colspan="5" class="muted">' + esc(d.error || '加载失败') + '</td></tr>';
      return;
    }
    if (!d.invites.length) {
      body.innerHTML = '<tr><td colspan="5" class="muted">还没有邀请码，用上面的表单生成一批</td></tr>';
      return;
    }
    var now = Math.floor(Date.now() / 1000);
    var html = '';
    d.invites.forEach(function (it) {
      var st = inviteState(it, now);
      html += '<tr><td><code>' + esc(it.code) + '</code></td>'
        + '<td class="num">' + it.used_count + ' / ' + it.max_uses + '</td>'
        + '<td class="num">' + esc(inviteExpiry(it, now)) + '</td>'
        + '<td><span class="pill ' + st.cls + '">' + st.text + '</span></td>'
        + '<td><button class="ghost sm copy" type="button" data-code="' + esc(it.code) + '">复制</button></td>'
        + '</tr>';
    });
    body.innerHTML = html;
  }).catch(function () {
    $('invite-body').innerHTML = '<tr><td colspan="5" class="muted">网络错误</td></tr>';
  });
}
$('invite-body').addEventListener('click', function (e) {
  var btn = e.target.closest ? e.target.closest('button.copy') : null;
  if (btn) copyText(btn.getAttribute('data-code'), btn, '复制');
});

/* ── 通道 ── */
function renderChannels(d) {
  var list = $('ch-list');
  var chans = d.channels || [];
  if (!chans.length) {
    list.innerHTML = '<p class="muted">' + esc(d.note || '没有可显示的通道') + '</p>';
    return;
  }
  var html = '';
  chans.forEach(function (c) {
    var bal = c.balance == null ? '查不到' : String(c.balance);
    html += '<div class="chan">'
      + '<div class="head"><div><b>' + esc(c.name) + '</b> <span class="badge">' + esc(c.code) + '</span></div>'
      + '<div class="dot ' + (c.ok ? 'ok' : 'bad') + '">' + (c.ok ? '✓ 正常' : '✗ 不通') + '</div></div>'
      + '<div class="kv"><span>模型</span><b class="num" style="font-size:12px">' + esc(c.model || '未配置') + '</b></div>'
      + '<div class="kv"><span>延迟</span><b class="num">' + (c.ms || 0) + ' ms</b></div>'
      + '<div class="kv"><span>余额</span><b class="num">' + esc(bal) + '</b></div>'
      + '<div class="muted" style="margin-top:8px;font-size:12px">' + esc(c.balance_note || '') + '</div>'
      + (c.error ? '<div class="errbox">' + esc(c.error) + '</div>' : '')
      + '<div class="muted" style="margin-top:8px;font-size:12px;word-break:break-all">' + esc(c.baseUrl) + '</div>'
      + '</div>';
  });
  list.innerHTML = html;
}
function loadChannels() {
  var btn = $('btn-refresh-ch');
  setMsg('ch-note', '正在并发探测各通道（每个通道最多 3 秒）…');
  $('ch-list').innerHTML = '';
  busy(btn, true, '探测中…');
  get('/api/admin/channels').then(function (d) {
    busy(btn, false);
    if (!d.ok) { setMsg('ch-note', d.error || '加载失败', 'bad'); return; }
    state.channelsLoaded = true;
    var n = (d.channels || []).length;
    setMsg('ch-note', (d.note ? d.note : ('共 ' + n + ' 个通道 · 探测时间 ' + fmtDateTime(d.generated_at))), '');
    renderChannels(d);
  }).catch(function () { busy(btn, false); setMsg('ch-note', '网络错误', 'bad'); });
}
$('btn-refresh-ch').onclick = function () { loadChannels(); };

/* 回车直接提交 */
$('li-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('btn-login').click(); });
$('rg-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('btn-register').click(); });

loadMe();
</script>
</body>
</html>
`;
