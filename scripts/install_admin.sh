#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
ADMIN_ROOT="${ADMIN_ROOT:-$PROJECT_ROOT/admin}"
RELEASE_ROOT="${RELEASE_ROOT:-$PROJECT_ROOT/runtime/admin/releases}"
NODE_VERSION="${NODE_VERSION:-v22.23.2}"
NODE_PREFIX="${NODE_PREFIX:-/home/donxu/.local/node-$NODE_VERSION}"
NODE_LINK="/home/donxu/.local/node"
UNIT_DIR="/home/donxu/.config/systemd/user"

if [[ ! -x "$NODE_PREFIX/bin/node" ]]; then
  temporary="$(mktemp -d)"
  trap 'rm -rf "$temporary"' EXIT
  archive="node-$NODE_VERSION-linux-x64.tar.xz"
  base="https://nodejs.org/dist/$NODE_VERSION"
  curl -fsSLo "$temporary/$archive" "$base/$archive"
  curl -fsSLo "$temporary/SHASUMS256.txt" "$base/SHASUMS256.txt"
  (
    cd "$temporary"
    grep "  $archive$" SHASUMS256.txt | sha256sum -c -
  )
  mkdir -p "$NODE_PREFIX"
  tar -xJf "$temporary/$archive" --strip-components=1 -C "$NODE_PREFIX"
fi

ln -sfn "$NODE_PREFIX" "$NODE_LINK"
export PATH="$NODE_LINK/bin:$PATH"
mkdir -p "$PROJECT_ROOT/runtime/admin"
if [[ ! -s "$PROJECT_ROOT/runtime/admin/admin.env" ]]; then
  umask 077
  initial_password="$("$NODE_LINK/bin/node" -e 'process.stdout.write(require("node:crypto").randomBytes(24).toString("base64url"))')"
  password_hash="$("$NODE_LINK/bin/node" -e 'const c=require("node:crypto");const s=c.randomBytes(16).toString("hex");process.stdout.write(s+":"+c.scryptSync(process.argv[1],s,64).toString("hex"))' "$initial_password")"
  session_secret="$("$NODE_LINK/bin/node" -e 'process.stdout.write(require("node:crypto").randomBytes(64).toString("hex"))')"
  {
    printf 'ADMIN_USERNAME=admin\n'
    printf 'ADMIN_PASSWORD_HASH=%s\n' "$password_hash"
    printf 'ADMIN_SESSION_SECRET=%s\n' "$session_secret"
    printf 'ADMIN_PUBLIC_ORIGIN=https://aicentre2.sligenai.cn:8443\n'
    printf 'ADMIN_AUDIT_LOG=/home/donxu/ai-centre/runtime/admin/audit.jsonl\n'
    printf 'ADMIN_UPLOAD_DIR=/home/donxu/ai-centre/runtime/admin/uploads\n'
    printf 'AI_CENTRE_CONTROL_URL=http://127.0.0.1:8320\n'
    printf 'AI_CENTRE_FACE_URL=http://127.0.0.1:8310\n'
    printf 'AI_CENTRE_OCR_URL=http://127.0.0.1:8096\n'
    printf 'AI_CENTRE_SUBTITLE_URL=http://127.0.0.1:8097\n'
  } > "$PROJECT_ROOT/runtime/admin/admin.env"
  printf '%s\n' "$initial_password" > "$PROJECT_ROOT/runtime/admin/initial-admin-password.txt"
  chmod 0600 "$PROJECT_ROOT/runtime/admin/admin.env" "$PROJECT_ROOT/runtime/admin/initial-admin-password.txt"
fi

cd "$ADMIN_ROOT"
"$NODE_LINK/bin/npm" ci
"$NODE_LINK/bin/npm" run build

# Never serve directly from ADMIN_ROOT/.next. A later `next build` removes that
# directory before rebuilding and would make the live CSS/JS disappear.
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
release_dir="$RELEASE_ROOT/$release_id"
mkdir -p "$release_dir/.next/static"
cp -a .next/standalone/. "$release_dir/"
cp -a .next/static/. "$release_dir/.next/static/"
if [[ -d public ]]; then
  mkdir -p "$release_dir/public"
  cp -a public/. "$release_dir/public/"
fi
ln -sfn "$release_dir" "$PROJECT_ROOT/runtime/admin/current.next"
mv -Tf "$PROJECT_ROOT/runtime/admin/current.next" "$PROJECT_ROOT/runtime/admin/current"

mkdir -p "$UNIT_DIR"
install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-admin.service" \
  "$UNIT_DIR/ai-centre-admin.service"
mkdir -p "$UNIT_DIR/ai-centre-admin.service.d"
install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-admin.service.d/production-release.conf" \
  "$UNIT_DIR/ai-centre-admin.service.d/production-release.conf"
systemctl --user daemon-reload
systemctl --user enable ai-centre-admin.service
systemctl --user restart ai-centre-admin.service
