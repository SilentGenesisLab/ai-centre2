#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
CADDY_VERSION="${CADDY_VERSION:-2.11.4}"
CADDY_SHA512="${CADDY_SHA512:-8220d1f013b6f27510247b2360c9e0ca9f018feebd82515f07635318b34ff9777ccc8fd0b6e6f2486ce3a33fe389fbb7db12d05baa474f4587509fb4f5ebf1c9}"
CADDY_BIN="${CADDY_BIN:-/home/donxu/.local/bin/caddy}"
CADDY_ARCHIVE="${CADDY_ARCHIVE:-}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ARCHIVE_NAME="caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
DOWNLOAD_URL="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/${ARCHIVE_NAME}"
TEMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

test -d "$PROJECT_ROOT"
mkdir -p \
  "$(dirname "$CADDY_BIN")" \
  "$PROJECT_ROOT/runtime/caddy/logs" \
  "$PROJECT_ROOT/runtime/caddy/data" \
  "$PROJECT_ROOT/runtime/caddy/config" \
  "$USER_UNIT_DIR"

if [[ -n "$CADDY_ARCHIVE" ]]; then
  cp "$CADDY_ARCHIVE" "$TEMP_DIR/$ARCHIVE_NAME"
else
  curl --fail --location --silent --show-error \
    "$DOWNLOAD_URL" \
    --output "$TEMP_DIR/$ARCHIVE_NAME"
fi
printf '%s  %s\n' \
  "$CADDY_SHA512" \
  "$TEMP_DIR/$ARCHIVE_NAME" \
  | sha512sum --check -

tar -xzf "$TEMP_DIR/$ARCHIVE_NAME" -C "$TEMP_DIR" caddy
install -m 0755 "$TEMP_DIR/caddy" "$CADDY_BIN"
"$CADDY_BIN" validate \
  --config "$PROJECT_ROOT/gateway/Caddyfile" \
  --adapter caddyfile

install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-caddy.service" \
  "$USER_UNIT_DIR/ai-centre-caddy.service"

systemctl --user daemon-reload
systemctl --user enable --now ai-centre-caddy.service
