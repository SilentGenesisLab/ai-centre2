#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
UV_BIN="${UV_BIN:-/home/donxu/.local/bin/uv}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

test -d "$PROJECT_ROOT"
test -x "$UV_BIN"

cd "$PROJECT_ROOT"
if [[ ! -x .venv-control/bin/python ]]; then
  "$UV_BIN" venv --python 3.11 --seed .venv-control
fi
"$UV_BIN" pip install \
  --python .venv-control/bin/python \
  -r requirements-control.txt

mkdir -p runtime/control "$USER_UNIT_DIR"
install -m 0644 \
  deploy/systemd-user/ai-centre-control.service \
  "$USER_UNIT_DIR/ai-centre-control.service"
install -m 0644 \
  deploy/systemd-user/ai-centre-tts-worker.service \
  "$USER_UNIT_DIR/ai-centre-tts-worker.service"

systemctl --user daemon-reload
systemctl --user enable ai-centre-control.service
systemctl --user enable ai-centre-tts-worker.service
systemctl --user restart ai-centre-control.service
systemctl --user restart ai-centre-tts-worker.service
