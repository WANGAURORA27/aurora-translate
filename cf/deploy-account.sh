#!/usr/bin/env bash
# 部署账户系统（阶段 1）：建 D1 → 建 KV → 灌表结构 → 写密钥 → 部署 → 绑 account.ourmetaverse.cn
#
# 需要 Cloudflare API Token 具备：Workers Scripts:Edit、Workers KV:Edit、**D1:Edit**、Workers Routes:Edit
# 用法：bash cf/deploy-account.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # cf/
ACCOUNT_DIR="$HERE/account"
REPO="$(cd "$HERE/.." && pwd)"
SECRETS="$REPO/.secrets"
HOST="${1:-account.ourmetaverse.cn}"
DB_NAME="aurora-account"

REAL_HOME="$HOME"
CFTOOLS="$(cd "$REPO/.." && pwd)/cf-tools"
if [ -x "$CFTOOLS/node_modules/.bin/wrangler" ]; then
  WRANGLER="$CFTOOLS/node_modules/.bin/wrangler"
  export XDG_CONFIG_HOME="$CFTOOLS/xdg/config" XDG_CACHE_HOME="$CFTOOLS/xdg/cache"
  export XDG_DATA_HOME="$CFTOOLS/xdg/data" HOME="$CFTOOLS/xdg/home"
  mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$HOME"
else
  WRANGLER="$(command -v wrangler)"
fi
export WRANGLER_SEND_METRICS=false
wf() { ( cd "$ACCOUNT_DIR" && "$WRANGLER" "$@" ); }

step() { printf '\n\033[1;34m== %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die()  { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

mkdir -p "$SECRETS"
[ -f "$SECRETS/cf_token" ] || die "缺少 $SECRETS/cf_token"
export CLOUDFLARE_API_TOKEN="$(tr -d ' \t\r\n' < "$SECRETS/cf_token")"

step "1/7 验证 Cloudflare 凭据"
wf whoami >/dev/null 2>&1 || die "Token 无效，先跑 wrangler whoami 看报错"
ok "凭据可用 ✓"

step "2/7 建 D1 数据库"
if grep -q "REPLACE_WITH_D1_ID" "$ACCOUNT_DIR/wrangler.toml"; then
  OUT=$(wf d1 create "$DB_NAME" 2>&1) || { echo "$OUT"; die "D1 创建失败（令牌可能缺少 D1:Edit 权限）"; }
  echo "$OUT" | tail -4
  DB_ID=$(printf '%s' "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
  [ -n "$DB_ID" ] || die "没能从输出里认出 database_id，请手动填进 cf/account/wrangler.toml"
  python3 - "$ACCOUNT_DIR/wrangler.toml" "$DB_ID" <<'PY'
import sys
path, db_id = sys.argv[1], sys.argv[2]
t = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(t.replace("REPLACE_WITH_D1_ID", db_id))
print("  已写入 database_id：", db_id)
PY
else
  echo "wrangler.toml 里已有 D1 id，跳过"
fi

step "3/7 建限流用的 KV"
if grep -q "REPLACE_WITH_LIMITS_ID" "$ACCOUNT_DIR/wrangler.toml"; then
  OUT=$(wf kv namespace create LIMITS 2>&1) || { echo "$OUT"; die "KV 创建失败"; }
  KV_ID=$(printf '%s' "$OUT" | grep -oE '[0-9a-f]{32}' | head -1)
  [ -n "$KV_ID" ] || die "没能认出 KV id"
  python3 - "$ACCOUNT_DIR/wrangler.toml" "$KV_ID" <<'PY'
import sys
path, kv_id = sys.argv[1], sys.argv[2]
t = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(t.replace("REPLACE_WITH_LIMITS_ID", kv_id))
print("  已写入 KV id：", kv_id)
PY
else
  echo "wrangler.toml 里已有 KV id，跳过"
fi

step "4/7 灌入表结构（可重复执行）"
wf d1 execute "$DB_NAME" --remote --file=schema.sql --yes 2>&1 | tail -4
ok "表结构就绪 ✓"

step "5/7 写密钥"
if [ ! -f "$SECRETS/account_session_secret" ]; then
  openssl rand -hex 32 > "$SECRETS/account_session_secret"
  chmod 600 "$SECRETS/account_session_secret"
  echo "已生成 SESSION_SECRET（$SECRETS/account_session_secret）"
fi
printf '%s' "$(tr -d ' \t\r\n' < "$SECRETS/account_session_secret")" | wf secret put SESSION_SECRET >/dev/null
ok "SESSION_SECRET 已写入 ✓"

if [ -f "$SECRETS/resend_key" ]; then
  printf '%s' "$(tr -d ' \t\r\n' < "$SECRETS/resend_key")" | wf secret put RESEND_KEY >/dev/null
  ok "RESEND_KEY 已写入 ✓（可以发验证码邮件了）"
else
  warn "没有 $SECRETS/resend_key —— 线上将无法发送验证码邮件"
  warn "去 https://resend.com 免费注册（3000 封/月），把 API Key 写进那个文件后重跑本脚本"
fi

step "6/7 部署 Worker"
DEPLOY_LOG=$(wf deploy 2>&1) || true
printf '%s\n' "$DEPLOY_LOG" | tail -6
URL=$(printf '%s' "$DEPLOY_LOG" | grep -oE 'https://[a-z0-9.-]+\.workers\.dev' | head -1)
[ -n "$URL" ] || warn "没认出 workers.dev 地址（不影响绑自定义域名）"

step "7/7 绑定域名 $HOST"
bash "$HERE/set-domain.sh" "$HOST" aurora-account 2>&1 | tail -12
printf '%s' "$HOST" > "$SECRETS/account_domain"

cat <<EOF

──────────────────────────────────────────────
账户系统部署完成

  注册登录   https://$HOST
  数据       Cloudflare D1（$DB_NAME）
  发信       $([ -f "$SECRETS/resend_key" ] && echo "已配置（Resend）" || echo "未配置 —— 验证码发不出去")

  ⚠️ 第一个注册的账号会自动成为管理员，建议你立刻去注册。
──────────────────────────────────────────────
EOF
