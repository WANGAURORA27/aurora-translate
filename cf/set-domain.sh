#!/usr/bin/env bash
# 把 doc.ourmetaverse.cn 绑到网页端 Worker，并从本机实测"国内能不能直连"
#
# 前置：
#   1. 域名已加入 Cloudflare 账号（面板 Add a site）
#   2. 已在阿里云把 NS 改成 Cloudflare 给的那两个（改完要等生效）
#   3. Cloudflare API Token 要有 Zone:DNS:Edit（能读 zone 更好，没有就把 zone id 填进
#      .secrets/cf_zone_id，面板站点首页右侧"Zone ID"可复制）
#
# 用法：bash cf/set-domain.sh [hostname]      默认 doc.ourmetaverse.cn
set -euo pipefail

HOST="${1:-doc.ourmetaverse.cn}"
WORKER_NAME_ARG="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SECRETS="$REPO/.secrets"
# Worker 名字：第二个参数优先，否则默认翻译站
WORKER_NAME="aurora-translate"
[ -n "$WORKER_NAME_ARG" ] && WORKER_NAME="$WORKER_NAME_ARG"
ACCOUNT_ID="873e57a00b1805cf864e89ea563ec072"

ok()   { printf '\033[32m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die()  { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

[ -f "$SECRETS/cf_token" ] || die "缺少 $SECRETS/cf_token"
T="$(tr -d ' \t\r\n' < "$SECRETS/cf_token")"
api() {  # api 方法 路径 [数据]
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -s -X "$method" -H "Authorization: Bearer $T" -H 'content-type: application/json' \
      -d "$data" "https://api.cloudflare.com/client/v4$path"
  else
    curl -s -X "$method" -H "Authorization: Bearer $T" "https://api.cloudflare.com/client/v4$path"
  fi
}
jq_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

ZONE="${HOST#*.}"                      # doc.ourmetaverse.cn → ourmetaverse.cn
printf '\n\033[1;34m== 1/5 找到域名 %s 的 zone ==\033[0m\n' "$ZONE"
ZONE_ID=""
ZONE_STATUS="?"
if [ -f "$SECRETS/cf_zone_id" ]; then
  ZONE_ID="$(tr -d ' \t\r\n' < "$SECRETS/cf_zone_id")"
  echo "  用 .secrets/cf_zone_id 里的：$ZONE_ID"
else
  ZONE_ID=$(api GET "/zones?name=$ZONE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
r=d.get('result') or []
print(r[0]['id'] if r else '')" 2>/dev/null || true)
  if [ -n "$ZONE_ID" ]; then
    printf '%s' "$ZONE_ID" > "$SECRETS/cf_zone_id"
    echo "  查到 zone id：${ZONE_ID}（已记入 .secrets/cf_zone_id）"
  fi
fi

if [ -z "$ZONE_ID" ]; then
  warn "  令牌读不到 zone。请在 Cloudflare 面板打开这个站点，右侧「Zone ID」点复制，"
  warn "  然后写进文件：printf '%s' '粘贴的ID' > \"$SECRETS/cf_zone_id\"，再重跑本脚本。"
  warn "  或者给令牌加上 Zone:Read 权限。"
  ZONE_STATUS="?"
else
  ZONE_STATUS=$(api GET "/zones/$ZONE_ID" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(((d.get('result') or {}).get('status')) or '?')" 2>/dev/null || echo "?")
  echo "  zone 状态：$ZONE_STATUS"
fi

printf '\n\033[1;34m== 2/5 检查 NS 是否已经切到 Cloudflare ==\033[0m\n'
NS_NOW=$(dig +short NS "$ZONE" @8.8.8.8 2>/dev/null | tr '\n' ' ')
[ -n "$NS_NOW" ] || NS_NOW=$(dig +short NS "$ZONE" 2>/dev/null | tr '\n' ' ')
echo "  当前 NS：${NS_NOW:-（查询失败）}"
case "$NS_NOW" in
  *cloudflare.com*) ok "  已指向 Cloudflare ✓" ;;
  *) warn "  还不是 Cloudflare 的 NS —— 请先在阿里云改 NS（改完等 10 分钟~几小时）" ;;
esac
[ "$ZONE_STATUS" = "active" ] && ok "  Cloudflare 已确认接管（active）✓" || warn "  Cloudflare 还没确认接管（状态：${ZONE_STATUS}）"

if [ "$ZONE_STATUS" != "active" ]; then
  printf '\n\033[33mNS 还没生效，先不做后面的绑定。等生效后重跑本脚本即可。\033[0m\n'
  exit 0
fi

printf '\n\033[1;34m== 3/5 清掉指向已下线服务器的旧记录 ==\033[0m\n'
RECS=$(api GET "/zones/$ZONE_ID/dns_records?per_page=100")
if ! printf '%s' "$RECS" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
  warn "  令牌没有 Zone:DNS:Edit 权限，跳过自动清理"
  warn "  绑定时若报「记录已存在」，请到面板 DNS 页面手动删掉 doc 那条，再重跑"
  : > /tmp/aurora_del.txt
else
printf '%s' "$RECS" | AURORA_HOST="$HOST" python3 - <<'PY' > /tmp/aurora_del.txt
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
want = os.environ.get("AURORA_HOST", "")
for r in d.get("result") or []:
    if r.get("type") in ("A", "AAAA", "CNAME") and r.get("name") == want:
        print(r["id"], r.get("content"))
PY
fi
if [ -s /tmp/aurora_del.txt ]; then
  while read -r rid content; do
    echo "  删除旧记录 $HOST → $content"
    api DELETE "/zones/$ZONE_ID/dns_records/$rid" > /dev/null
  done < /tmp/aurora_del.txt
else
  echo "  没有需要清理的旧 A 记录"
fi

printf '\n\033[1;34m== 4/5 把 %s 绑到 Worker %s ==\033[0m\n' "$HOST" "$WORKER_NAME"
EXIST=$(api GET "/accounts/$ACCOUNT_ID/workers/domains" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for x in d.get('result') or []:
    if x.get('hostname') == '$HOST':
        print(x.get('id') or 'x'); break" 2>/dev/null || true)
if [ -n "$EXIST" ]; then
  echo "  已经绑定过了，跳过"
else
  OUT=$(api PUT "/accounts/$ACCOUNT_ID/workers/domains" \
    "{\"zone_id\":\"$ZONE_ID\",\"hostname\":\"$HOST\",\"service\":\"$WORKER_NAME\",\"environment\":\"production\"}")
  printf '%s' "$OUT" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('success'):
    r=d.get('result') or {}
    print('  绑定成功：%s → %s' % (r.get('hostname'), r.get('service')))
else:
    for e in (d.get('errors') or [])[:2]: print('  失败 %s: %s' % (e.get('code'), e.get('message')))
    sys.exit(1)"
fi

printf '\n\033[1;34m== 5/5 从本机实测能不能直连（关键一步）==\033[0m\n'
sleep 20
IPS=$(dig +short "$HOST" 2>/dev/null | tr '\n' ' ')
PUB=$(dig +short "$HOST" @1.1.1.1 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
echo "  本机解析到：${IPS:-（空，多半是本地缓存旧记录）}"
echo "  公共 DNS 解析到：${PUB:-（空）}"
if [ -n "$PUB" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --resolve "$HOST:443:$PUB" "https://$HOST/" 2>/dev/null || echo 000)
else
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$HOST/" 2>/dev/null || echo 000)
fi
echo "  HTTPS 请求：HTTP $code"
if [ "$code" = "200" ]; then
  ok "  ✓ 本机能直连！国内可用（无需代理）"
else
  warn "  直连失败（HTTP ${code}）。看握手细节："
  EXTRA=""; [ -n "$PUB" ] && EXTRA="--resolve $HOST:443:$PUB"
  curl -sv --max-time 15 -o /dev/null $EXTRA "https://$HOST/" 2>&1 | \
    grep -E "Trying|Connected|TLS|SSL|Recv failure|reset|HTTP/" | head -6 | sed 's/^/    /'
  warn "  若出现 'Recv failure: Connection reset by peer'，说明 Cloudflare 这批 IP 也被 SNI 阻断。"
fi
