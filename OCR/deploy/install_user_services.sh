#!/usr/bin/env bash
set -euo pipefail

OCR_ROOT="${OCR_ROOT:-/home/donxu/ai-centre/OCR}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$USER_UNIT_DIR"
install -m 0644 \
  "$OCR_ROOT/deploy/systemd-user/ai-centre-ocr-gateway.service" \
  "$USER_UNIT_DIR/ai-centre-ocr-gateway.service"
install -m 0644 \
  "$OCR_ROOT/deploy/systemd-user/ai-centre-ocr-worker@.service" \
  "$USER_UNIT_DIR/ai-centre-ocr-worker@.service"

systemctl --user daemon-reload
systemctl --user enable --now ai-centre-ocr-worker@0.service
systemctl --user enable --now ai-centre-ocr-worker@1.service
systemctl --user enable --now ai-centre-ocr-gateway.service
