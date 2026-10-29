#!/usr/bin/env bash
# 续期 aicentre2.sligenai.cn 的 TLS 证书（DNS-01 / 阿里云云解析）。
#
#   bash scripts/renew_tls_cert.sh           # 到期才续（给定时器用）
#   bash scripts/renew_tls_cert.sh --force   # 立即重签（手工救急用）
#
# 为什么是 DNS-01 而不是 HTTP-01：这台机器在小米路由器后面，公网 80/443 被路由器
# 自己占着，Let's Encrypt 的 HTTP-01 打不到 Caddy（见 docs/HTTPS_GATEWAY.md）。
#
# 凭据只从 .env 读，**不落命令行、不进仓库**：
#   ALI_DNS_ACCESS_KEY_ID / ALI_DNS_ACCESS_KEY_SECRET
# 单独一对变量而不是复用 OSS 的那对 —— 云解析是写权限，不该跟存图的钥匙共用。
# 缺变量时安静退出（退出码 0）：定时器每天跑，没配好不该天天报错。
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
ACME_HOME="${ACME_HOME:-$PROJECT_ROOT/runtime/acme-home}"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"
DOMAIN="${DOMAIN:-aicentre2.sligenai.cn}"
FORCE="${1:-}"

log() { printf '[renew-tls] %s\n' "$*"; }

if [ ! -x "$ACME_HOME/acme.sh" ]; then
  log "acme.sh 未安装（$ACME_HOME/acme.sh），跳过"
  exit 0
fi
if [ ! -f "$ENV_FILE" ]; then
  log "缺 $ENV_FILE，跳过"
  exit 0
fi

Ali_Key="$(grep -m1 '^ALI_DNS_ACCESS_KEY_ID=' "$ENV_FILE" | cut -d= -f2- || true)"
Ali_Secret="$(grep -m1 '^ALI_DNS_ACCESS_KEY_SECRET=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "$Ali_Key" ] || [ -z "$Ali_Secret" ]; then
  log "未配置 ALI_DNS_ACCESS_KEY_ID/SECRET，跳过（没有它能自动续期）"
  exit 0
fi
export Ali_Key Ali_Secret

common=(--home "$ACME_HOME" --config-home "$ACME_HOME" --cert-home "$ACME_HOME")
reload="systemctl --user restart ai-centre-caddy.service"

if [ "$FORCE" = "--force" ]; then
  log "强制重签 $DOMAIN"
  "$ACME_HOME/acme.sh" --issue "${common[@]}" \
    --server letsencrypt --dns dns_ali \
    -d "$DOMAIN" --keylength ec-256 \
    --reloadcmd "$reload" --force
else
  # 只续本域，**不要用 --cron**：那个会把 home 里所有证书都续一遍，
  # 而同一目录下还有 videocopy.sligenai.cn 这种没有配 DNS API 的（手动模式），
  # 它一失败整个 timer 就变成 failed，本域续不续得上反而被掩盖。
  log "检查续期 $DOMAIN（到期前 30 天内才会真正续）"
  # acme.sh 在「还没到期」时也返回非零，所以不能只看退出码 —— 那会把
  # 「今天不用续」误报成失败，天天刷一条假警报。按输出分辨两种情况。
  set +e
  output="$("$ACME_HOME/acme.sh" --renew "${common[@]}" \
    -d "$DOMAIN" --ecc \
    --server letsencrypt --dns dns_ali \
    --reloadcmd "$reload" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "$output" | tail -12
  if [ $status -eq 0 ]; then
    log "续期完成"
  elif printf '%s' "$output" | grep -q "Skipping"; then
    log "未到期，本次无需续期"
  else
    log "续期失败（退出码 $status），见上面的 acme.sh 输出"
    exit $status
  fi
fi

log "当前证书："
openssl x509 -in "$ACME_HOME/${DOMAIN}_ecc/fullchain.cer" -noout -dates -subject 2>/dev/null || true
