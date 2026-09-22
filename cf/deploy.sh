#!/usr/bin/env bash
# 一条命令把网页端部署上去（可重复跑：已存在的资源会跳过）
#
# 前置：
#   1. .secrets/cf_token 里有 Cloudflare API Token（权限见 cf/README.md）
#   2. gh 已登录，且当前目录是 aurora-translate 仓库
#   3. .secrets/password 里写你给使用者用的口令（没有则自动生成一个并打印）
#
# 用法：bash cf/deploy.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SECRETS="$REPO/.secrets"
BUCKET="aurora-files"

# wrangler 会往 ~/Library/Preferences 写日志，沙箱里没权限；统一指到工作区
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

mkdir -p "$SECRETS"
step() { printf '\n\033[1;34m== %s\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

[ -f "$SECRETS/cf_token" ] || die "缺少 $SECRETS/cf_token"
export CLOUDFLARE_API_TOKEN="$(tr -d ' \t\r\n' < "$SECRETS/cf_token")"

step "1/6 验证 Cloudflare 凭据"
"$WRANGLER" whoami >/dev/null 2>&1 || die "Token 无效或权限不足，先跑：\"$WRANGLER\" whoami 看详细报错"
echo "凭据可用 ✓"

step "2/6 建 KV（存任务进度）"
if grep -q "REPLACE_WITH_KV_ID" "$HERE/wrangler.toml"; then
  OUT=$("$WRANGLER" kv namespace create JOBS 2>&1) || { echo "$OUT"; die "KV 创建失败"; }
  echo "$OUT"
  KV_ID=$(printf '%s' "$OUT" | grep -oE '[0-9a-f]{32}' | head -1)
  [ -n "$KV_ID" ] || die "没能从输出里认出 KV id，请手动填进 cf/wrangler.toml"
  python3 - "$HERE/wrangler.toml" "$KV_ID" <<'PY'
import sys
path, kv_id = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(text.replace("REPLACE_WITH_KV_ID", kv_id))
print("已写入 KV id：", kv_id)
PY
else
  echo "wrangler.toml 里已有 KV id，跳过"
fi

step "3/6 建 R2 桶（存原件与译文）"
if "$WRANGLER" r2 bucket create "$BUCKET" 2>&1 | tee /tmp/aurora-r2.log | grep -qi "already exists"; then
  echo "桶已存在，跳过"
else
  tail -3 /tmp/aurora-r2.log
fi

step "4/6 写三个密钥"
# 内部密钥：网页端与 GitHub Actions 之间用，自动生成
if [ ! -f "$SECRETS/agent_key" ]; then
  openssl rand -hex 24 > "$SECRETS/agent_key"
  chmod 600 "$SECRETS/agent_key"
  echo "已生成内部密钥 $SECRETS/agent_key"
fi
AGENT_KEY="$(tr -d ' \t\r\n' < "$SECRETS/agent_key")"

# 使用口令：你来定，没有就生成一个
if [ ! -f "$SECRETS/password" ]; then
  openssl rand -base64 12 | tr -d '/+=' | cut -c1-14 > "$SECRETS/password"
  chmod 600 "$SECRETS/password"
  echo "已生成一个使用口令，请记下来（也可以自己改 $SECRETS/password 后重跑本脚本）"
fi
PASSWORD="$(tr -d '\r\n' < "$SECRETS/password")"

# 触发 Actions 用的 GitHub 令牌
if [ -f "$SECRETS/gh_pat" ]; then
  GH_TOKEN_VALUE="$(tr -d ' \t\r\n' < "$SECRETS/gh_pat")"
else
  GH_TOKEN_VALUE="$(gh auth token 2>/dev/null || true)"
fi
[ -n "$GH_TOKEN_VALUE" ] || die "没有可用的 GitHub 令牌：先 gh auth login，或把细粒度 PAT 放进 $SECRETS/gh_pat"

printf '%s' "$PASSWORD"   | "$WRANGLER" secret put PASSWORD  >/dev/null
printf '%s' "$AGENT_KEY"  | "$WRANGLER" secret put AGENT_KEY >/dev/null
printf '%s' "$GH_TOKEN_VALUE" | "$WRANGLER" secret put GH_TOKEN >/dev/null
echo "PASSWORD / AGENT_KEY / GH_TOKEN 已写入 ✓"

step "5/6 部署 Worker"
DEPLOY_LOG=$(cd "$HERE" && "$WRANGLER" deploy 2>&1)
printf '%s\n' "$DEPLOY_LOG" | tail -6
URL=$(printf '%s' "$DEPLOY_LOG" | grep -oE 'https://[a-z0-9.-]+\.workers\.dev' | head -1)
[ -n "$URL" ] || die "没能从部署输出里认出网址，请手动看一眼上面的输出"
echo "网址：$URL"

step "6/6 把网址与内部密钥同步给 GitHub Actions"
gh variable set WORKER_URL --body "$URL" --repo "$(gh repo view --json nameWithOwner -q .nameWithOwner)" 2>/dev/null \
  || gh variable set WORKER_URL --body "$URL"
printf '%s' "$AGENT_KEY" | gh secret set AGENT_KEY
echo "WORKER_URL / AGENT_KEY 已同步 ✓"

cat <<EOF

──────────────────────────────────────────────
部署完成

  网页地址   $URL
  使用口令   $PASSWORD
  内部密钥   $SECRETS/agent_key（不要外传，也不要提交）

接下来：
  1. 浏览器打开上面的网址，输入口令，传一份 PDF 试一遍
  2. 想改口令：改 $SECRETS/password 后重跑本脚本
  3. 想让朋友用：把网址和口令给他即可（译文 7 天后自动清理）
──────────────────────────────────────────────
EOF
