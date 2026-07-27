#!/usr/bin/env bash
set -euo pipefail

CONTROL_URL="${CONTROL_URL:-http://127.0.0.1:8320}"
SERVICE_TOKEN="${SERVICE_TOKEN:?SERVICE_TOKEN is required}"

curl -fsS -X POST \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  "$CONTROL_URL/v1/admin/gpus/0/disable" | python3 -m json.tool

nvidia-smi \
  --query-compute-apps=gpu_uuid,pid,process_name,used_memory \
  --format=csv,noheader

