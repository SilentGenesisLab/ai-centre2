#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
UV_BIN="${UV_BIN:-/home/donxu/.local/bin/uv}"
VENV_DIR="$PROJECT_ROOT/.venv-tts-v026"
SOURCE_ARCHIVE="$PROJECT_ROOT/runtime/vendor/vllm-omni-v0.26.0-source.tgz"
SOURCE_SHA256="83dc708f902eca84eb6785929073c94884ed6f27a64f47fde603416d0dba26cb"
STABILITY_WHEEL="$PROJECT_ROOT/vendor/wheels/vllm_omni-0.26.0+sligen1-py3-none-any.whl"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

test -d "$PROJECT_ROOT"
test -x "$UV_BIN"
mkdir -p "$USER_UNIT_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$UV_BIN" venv --python 3.12 --seed "$VENV_DIR"
fi

"$UV_BIN" pip install \
  --python "$VENV_DIR/bin/python" \
  "vllm==0.26.0" \
  --torch-backend=auto
test -f "$SOURCE_ARCHIVE"
printf '%s  %s\n' "$SOURCE_SHA256" "$SOURCE_ARCHIVE" | sha256sum --check
SETUPTOOLS_SCM_PRETEND_VERSION=0.26.0 \
VLLM_OMNI_TARGET_DEVICE=cuda \
  "$UV_BIN" pip install \
    --python "$VENV_DIR/bin/python" \
    "$SOURCE_ARCHIVE"
test -f "$STABILITY_WHEEL"
"$UV_BIN" pip install \
  --python "$VENV_DIR/bin/python" \
  --reinstall \
  --no-deps \
  "$STABILITY_WHEEL"
"$UV_BIN" pip install \
  --python "$VENV_DIR/bin/python" \
  "voxcpm==2.0.3" \
  "httpx>=0.27,<1" \
  "ninja>=1.11" \
  "soundfile>=0.13.1"

"$VENV_DIR/bin/python" -m pip show torch vllm vllm-omni voxcpm \
  | grep -E '^(Name|Version):'

install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-voxcpm2-v026-test.service" \
  "$USER_UNIT_DIR/ai-centre-voxcpm2-v026-test.service"
systemctl --user daemon-reload

echo "vLLM-Omni v0.26.0 test environment installed; service was not started."
