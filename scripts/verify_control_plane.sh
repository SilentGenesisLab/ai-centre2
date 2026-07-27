#!/usr/bin/env bash
set -euo pipefail

CONTROL_URL="${CONTROL_URL:-http://127.0.0.1:8320}"
SERVICE_TOKEN="${SERVICE_TOKEN:?SERVICE_TOKEN is required}"

curl -fsS "$CONTROL_URL/health" | python3 -m json.tool
curl -fsS \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  "$CONTROL_URL/v1/admin/gpus" | python3 -m json.tool

