#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
set -a
. "$PROJECT_ROOT/.env"
set +a
token_value="${SERVICE_TOKEN:-${AI_CENTRE_SERVICE_TOKEN:-}}"

systemctl --user start ai-centre-ocr-gateway.service
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8096/health >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8096/health >/dev/null

for _ in 1 2; do
  curl -fsS -X POST \
    -H "Authorization: Bearer $token_value" \
    -H "Content-Type: application/json" \
    -d '{"level":"l1","target_id":"ocr"}' \
    http://127.0.0.1:8320/internal/admin/health-monitor/run-check
  printf '\n'
  sleep 2
done
