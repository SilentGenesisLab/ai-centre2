#!/usr/bin/env bash
set -euo pipefail

CADDY_BIN="${CADDY_BIN:-/home/donxu/.local/bin/caddy}"
PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"

"$CADDY_BIN" validate \
  --config "$PROJECT_ROOT/gateway/Caddyfile" \
  --adapter caddyfile
systemctl --user is-active ai-centre-caddy.service
ss -lnt | grep -q ':18080 '
ss -lnt | grep -q ':18443 '
curl --fail --silent --show-error \
  --max-time 10 \
  http://127.0.0.1:8320/health \
  >/dev/null
