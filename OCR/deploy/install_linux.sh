#!/usr/bin/env bash
set -euo pipefail

OCR_ROOT="${OCR_ROOT:-/home/donxu/ai-centre/OCR}"
UV_BIN="${UV_BIN:-/home/donxu/.local/bin/uv}"

test -x "$UV_BIN"
cd "$OCR_ROOT"

if [[ ! -x .venv/bin/python ]]; then
  "$UV_BIN" venv --python 3.11 --seed .venv
fi
"$UV_BIN" pip install \
  --python .venv/bin/python \
  --index-url https://www.paddlepaddle.org.cn/packages/stable/cu129/ \
  paddlepaddle-gpu==3.3.0
"$UV_BIN" pip install --python .venv/bin/python -r requirements.txt

mkdir -p runtime/paddlex/official_models runtime/logs
.venv/bin/python - <<'PY'
import paddle
import paddleocr
import paddlex

assert paddle.device.is_compiled_with_cuda()
assert paddle.device.cuda.device_count() >= 2
print(
    "runtime_ok",
    {
        "paddle": paddle.__version__,
        "paddleocr": paddleocr.__version__,
        "paddlex": paddlex.__version__,
        "gpu_count": paddle.device.cuda.device_count(),
    },
)
PY
