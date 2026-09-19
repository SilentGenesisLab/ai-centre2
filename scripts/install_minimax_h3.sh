#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

test -d "$PROJECT_ROOT"
test -x "$PROJECT_ROOT/.venv-control/bin/celery"

mkdir -p "$PROJECT_ROOT/runtime/minimax-h3" "$USER_UNIT_DIR"
install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-h3-worker.service" \
  "$USER_UNIT_DIR/ai-centre-h3-worker.service"

systemctl --user daemon-reload
systemctl --user enable --now ai-centre-h3-worker.service
