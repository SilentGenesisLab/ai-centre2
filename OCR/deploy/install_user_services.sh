#!/usr/bin/env bash
set -euo pipefail

OCR_ROOT="${OCR_ROOT:-/home/donxu/ai-centre/OCR}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
OCR_WORKER_IDS="${OCR_WORKER_IDS:-1}"

mkdir -p "$USER_UNIT_DIR"
install -m 0644 \
  "$OCR_ROOT/deploy/systemd-user/ai-centre-ocr-gateway.service" \
  "$USER_UNIT_DIR/ai-centre-ocr-gateway.service"
install -m 0644 \
  "$OCR_ROOT/deploy/systemd-user/ai-centre-ocr-worker@.service" \
  "$USER_UNIT_DIR/ai-centre-ocr-worker@.service"

systemctl --user daemon-reload
for worker_id in $OCR_WORKER_IDS; do
  case "$worker_id" in
    0|1) systemctl --user enable --now "ai-centre-ocr-worker@$worker_id.service" ;;
    *) printf 'Unsupported OCR worker id: %s\n' "$worker_id" >&2; exit 2 ;;
  esac
done
systemctl --user enable --now ai-centre-ocr-gateway.service
