/**
 * 账户系统页面（单文件，Worker 直接吐给浏览器）
 * 注意：这是模板字符串，内部不要出现反引号和 ${，内联脚本一律用单引号拼接。
 */

export const PAGE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aurora 账户</title>
<style>
  :root { --ink:#16181d; --sub:#6b7280; --line:#e5e7eb; --brand:#2f6df6; --ok:#0f9d58; --bad:#d93025; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
         color:var(--ink); background:#f6f7f9; }
  .wrap { max-width:520px; margin:0 auto; padding:36px 20px 64px; }
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
  .row { display:flex; gap:10px; align-items:center; }
  .row > input { flex:1; }
  .tabs { display:flex; gap:8px; margin-bottom:14px; }
  .tabs button { flex:1; background:#eef1f6; color:var(--sub); }
  .tabs button.on { background:var(--brand); color:#fff; }
  .hidden { display:none !important; }
  .msg { font-size:14px; margin-top:14px; min-height:22px; }
  .msg.bad { color:var(--bad); }
  .msg.good { color:var(--ok); }
  .muted { color:var(--sub); font-size:13px; }
  .kv { display:flex; justify-content:space-between; padding:8px 0; border-top:1px solid var(--line); font-size:14px; }
  .kv:first-child { border-top:0; }
  .pill { display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px; background:#eef1f6; color:var(--sub); }
  .pill.admin { background:#fdecef; color:#c5221f; }
  .pill.vip { background:#fff4e5; color:#b06000; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--sub); font-weight:500; }
  code { background:#f1f3f7; padding:2px 6px; border-radius:5px; font-size:13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Aurora 账户</h1>
  <p class="lead">ourmetaverse.cn 的统一账号。一个账号，通行文档翻译等各项服务。</p>

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

  <!-- 已登录：个人中心 -->
  <div id="app" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div>
          <div style="font-size:16px;font-weight:600" id="me-email"></div>
          <div class="muted" id="me-role"></div>
        </div>
        <button id="btn-logout" class="ghost">退出登录</button>
      </div>
      <div style="margin-top:16px">
        <div class="kv"><span>本月可用页数</span><b id="me-quota">—</b></div>
        <div class="kv"><span>本月已用</span><b id="me-used">—</b></div>
        <div class="kv"><span>累计翻译</span><b id="me-total">—</b></div>
        <div class="kv"><span>注册时间</span><b id="me-created">—</b></div>
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
      <table>
        <thead><tr><th>时间</th><th>类型</th><th>页数</th></tr></thead>
        <tbody id="usage-body"><tr><td colspan="3" class="muted">暂无记录</td></tr></tbody>
      </table>
    </div>

    <div class="card hidden" id="admin-card">
      <div style="font-weight:600;margin-bottom:10px">管理员 · 用户管理</div>
      <table>
        <thead><tr><th>邮箱</th><th>角色</th><th>额度</th><th>已用</th><th>操作</th></tr></thead>
        <tbody id="admin-body"></tbody>
      </table>
      <p class="muted" style="margin-top:10px">点"改"可以调角色和额度；封号会把该用户所有会话踢下线。</p>
      <p class="msg" id="ad-msg"></p>
    </div>
  </div>
</div>

<script>
var $ = function (id) { return document.getElementById(id); };
function show(id, on) { $(id).classList.toggle('hidden', !on); }
function setMsg(id, text, kind) { var el = $(id); el.textContent = text || ''; el.className = 'msg' + (kind ? ' ' + kind : ''); }

function post(path, body) {
  return fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {})
  }).then(function (r) { return r.json().then(function (d) { d._status = r.status; return d; }); });
}
function get(path) {
  return fetch(path, { credentials: 'same-origin' }).then(function (r) {
    return r.json().then(function (d) { d._status = r.status; return d; });
  });
}
function busy(btn, on, label) {
  btn.disabled = on;
  if (on) { btn.dataset._t = btn.textContent; btn.textContent = label || '请稍候…'; }
  else if (btn.dataset._t) { btn.textContent = btn.dataset._t; }
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
    email: $('rg-email').value, code: $('rg-code').value, password: $('rg-pass').value
  }).then(function (d) {
    busy(btn, false);
    if (!d.ok) { setMsg('rg-msg', d.error || '注册失败', 'bad'); return; }
    $('rg-pass').value = ''; $('rg-code').value = '';
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
  post('/api/logout', {}).then(function () { loadMe(); });
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

/* ── 渲染 ── */
function fmtDate(sec) {
  if (!sec) return '—';
  var d = new Date(sec * 1000);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function loadMe() {
  get('/api/me').then(function (d) {
    if (!d.ok) {
      show('guest', true); show('app', false);
      if (d._status === 401) setMsg('li-msg', '');
      return;
    }
    show('guest', false); show('app', true);
    $('me-email').textContent = d.user.email;
    var role = d.user.role;
    $('me-role').innerHTML = '<span class="pill ' + role + '">' +
      (role === 'admin' ? '管理员' : role === 'vip' ? 'VIP' : '普通用户') + '</span>';
    $('me-quota').textContent = d.user.quota_pages === 0 ? '不限' : d.user.quota_pages + ' 页';
    $('me-used').textContent = d.user.used_pages + ' 页';
    $('me-total').textContent = (d.usage_total.jobs || 0) + ' 个文件';
    $('me-created').textContent = fmtDate(d.user.created_at);
    show('admin-card', role === 'admin');
    loadUsage();
    if (role === 'admin') loadAdmin();
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
      tr.innerHTML = '<td>' + fmtDate(it.created_at) + '</td><td>' + (it.note || it.kind) + '</td><td>' + it.pages + '</td>';
      body.appendChild(tr);
    });
  }).catch(function () {});
}

function loadAdmin() {
  get('/api/admin/users').then(function (d) {
    if (!d.ok) return;
    var body = $('admin-body');
    body.innerHTML = '';
    d.users.forEach(function (u) {
      var tr = document.createElement('tr');
      var roleTxt = u.role === 'admin' ? '管理员' : u.role === 'vip' ? 'VIP' : '普通';
      tr.innerHTML = '<td>' + u.email + (u.status === 'banned' ? ' <span class="pill admin">已停用</span>' : '') +
        '</td><td>' + roleTxt + '</td><td>' + (u.quota_pages === 0 ? '不限' : u.quota_pages) +
        '</td><td>' + u.used_pages + '</td><td></td>';
      var td = tr.lastChild;
      var b = document.createElement('button');
      b.className = 'ghost'; b.textContent = '改'; b.style.padding = '4px 10px'; b.style.fontSize = '13px';
      b.onclick = function () {
        var role = prompt('角色（user / vip / admin）：', u.role);
        if (role === null) return;
        var quota = prompt('每月可用页数（0 = 不限）：', u.quota_pages);
        if (quota === null) return;
        post('/api/admin/update', { user_id: u.id, role: role.trim(), quota_pages: Number(quota) })
          .then(function (r) {
            setMsg('ad-msg', r.ok ? '已更新 ' + u.email : (r.error || '失败'), r.ok ? 'good' : 'bad');
            loadAdmin();
          });
      };
      td.appendChild(b);
      body.appendChild(tr);
    });
  }).catch(function () {});
}

/* 回车直接提交 */
$('li-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('btn-login').click(); });
$('rg-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('btn-register').click(); });

loadMe();
</script>
</body>
</html>
`;
