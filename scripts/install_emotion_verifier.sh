#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
VENV_DIR="$PROJECT_ROOT/.venv-tts-v026"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
MODEL_CACHE="$PROJECT_ROOT/models/.cache/modelscope"

test -x "$VENV_DIR/bin/python"
mkdir -p "$MODEL_CACHE" "$USER_UNIT_DIR"

MODELSCOPE_CACHE="$MODEL_CACHE" "$VENV_DIR/bin/python" - <<'PY'
from funasr import AutoModel

AutoModel(
    model="iic/emotion2vec_plus_large",
    device="cpu",
    disable_update=True,
)
PY

install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-emotion-verifier.service" \
  "$USER_UNIT_DIR/ai-centre-emotion-verifier.service"
systemctl --user daemon-reload
systemctl --user enable --now ai-centre-emotion-verifier.service

curl --retry 120 --retry-delay 2 --retry-all-errors -fsS \
  http://127.0.0.1:8196/health
