#!/usr/bin/env bash
set -euo pipefail

OCR_URL="${OCR_URL:-http://127.0.0.1:8096}"
for _ in $(seq 1 60); do
  if curl -fsS "$OCR_URL/health" > /tmp/ai-centre-ocr-health.json &&
    python3 -c 'import json; assert json.load(open("/tmp/ai-centre-ocr-health.json"))["status"] == "ok"' 2>/dev/null; then
    break
  fi
  sleep 2
done

python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/ai-centre-ocr-health.json").read_text())
assert payload["status"] == "ok", payload
assert payload["healthy_workers"] == 2, payload
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
