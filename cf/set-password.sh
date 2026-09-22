#!/usr/bin/env bash
# 改网页端的使用口令（你自己定，随时可改，改完立刻生效，不用重新部署）
#
# 用法：
#   bash cf/set-password.sh                 # 交互式输入（推荐，不回显）
#   bash cf/set-password.sh '你想要的口令'    # 直接给（注意 shell 历史里会留下记录）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SECRETS="$REPO/.secrets"
REAL_HOME="$HOME"
CFTOOLS="$(cd "$REPO/.." && pwd)/cf-tools"

die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }
ok()  { printf '\033[32m%s\033[0m\n' "$1"; }

if [ -x "$CFTOOLS/node_modules/.bin/wrangler" ]; then
  WRANGLER="$CFTOOLS/node_modules/.bin/wrangler"
  export XDG_CONFIG_HOME="$CFTOOLS/xdg/config" XDG_CACHE_HOME="$CFTOOLS/xdg/cache"
  export XDG_DATA_HOME="$CFTOOLS/xdg/data" HOME="$CFTOOLS/xdg/home"
  mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$HOME"
else
  WRANGLER="$(command -v wrangler)"
fi
export WRANGLER_SEND_METRICS=false
wf() { ( cd "$HERE" && "$WRANGLER" "$@" ); }

mkdir -p "$SECRETS"
[ -f "$SECRETS/cf_token" ] || die "缺少 $SECRETS/cf_token（Cloudflare API Token）"
export CLOUDFLARE_API_TOKEN="$(tr -d ' \t\r\n' < "$SECRETS/cf_token")"

# gh 的登录态依赖真实 HOME，所以在覆盖 HOME 之前先把令牌取好
if [ -f "$SECRETS/gh_pat" ]; then
  GH_TOKEN_VALUE="$(tr -d ' \t\r\n' < "$SECRETS/gh_pat")"
else
  GH_TOKEN_VALUE="$(HOME="$REAL_HOME" gh auth token 2>/dev/null || true)"
fi
[ -n "$GH_TOKEN_VALUE" ] || die "取不到 GitHub 令牌：先 gh auth login"
export GH_TOKEN="$GH_TOKEN_VALUE"

PW="${1:-}"
if [ -z "$PW" ]; then
  printf '请输入新口令（输入时不显示）：'
  read -rs PW; echo
  printf '再输一遍确认：'
  read -rs PW2; echo
  [ "$PW" = "$PW2" ] || die "两次输入不一致，没有改动"
fi
[ "${#PW}" -ge 6 ] || die "口令太短了（至少 6 位；建议 10 位以上，别用你其他地方用过的密码）"

printf '%s' "$PW" > "$SECRETS/password"
chmod 600 "$SECRETS/password"
ok "已写入 $SECRETS/password"

printf '%s' "$PW" | wf secret put PASSWORD >/dev/null
ok "Cloudflare Worker 上的口令已更新（立即生效，无需重新部署）"

if printf '%s' "$PW" | gh secret set SITE_PASSWORD 2>/dev/null; then
  ok "GitHub 仓库的 SITE_PASSWORD 也同步了（线上自检要用它）"
else
  printf '\033[33m%s\033[0m\n' "提示：GitHub 那边没同步成功，Actions 里的线上自检可能因口令不符而失败"
fi

cat <<EOF

──────────────────────────────────────────────
口令已改好

  新口令   $PW
  生效范围 https://aurora-translate.wangaurora27.workers.dev

注意：
  * 已经打开页面的人，旧口令在当前标签页里还有效；关掉浏览器再开就要用新口令
  * 想知道现在用的是哪个口令：cat $SECRETS/password
──────────────────────────────────────────────
EOF
