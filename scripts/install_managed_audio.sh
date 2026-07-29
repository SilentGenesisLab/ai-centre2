#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
UV_BIN="${UV_BIN:-/home/donxu/.local/bin/uv}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VLLM_OMNI_TAG="${VLLM_OMNI_TAG:-v0.21.0rc2}"
VLLM_OMNI_COMMIT="${VLLM_OMNI_COMMIT:-5f9aee193eb0a1119fa6f5ba66ee7244daf6bc3c}"
VLLM_OMNI_DIR="$PROJECT_ROOT/runtime/vendor/vllm-omni"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
PYPI_MIRROR="${PYPI_MIRROR:-https://mirrors.aliyun.com/pypi/simple}"
export PROJECT_ROOT HF_ENDPOINT HF_HUB_DISABLE_XET

test -d "$PROJECT_ROOT"
test -x "$UV_BIN"

cd "$PROJECT_ROOT"
mkdir -p \
  models \
  runtime/control/tts-speakers \
  runtime/references \
  runtime/vendor \
  runtime/vllm \
  "$USER_UNIT_DIR"

if [[ ! -x .venv-asr/bin/python ]]; then
  "$UV_BIN" venv --python 3.11 --seed .venv-asr
fi
"$UV_BIN" pip install \
  --python .venv-asr/bin/python \
  -r requirements-asr.txt
.venv-asr/bin/python - <<'PY'
import os

from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="Systran/faster-whisper-large-v3",
    local_dir=f"{os.environ['PROJECT_ROOT']}/models/faster-whisper-large-v3",
)
PY

if [[ ! -x .venv-tts/bin/python ]]; then
  "$UV_BIN" venv --python 3.12 --seed .venv-tts
fi
"$UV_BIN" pip install \
  --python .venv-tts/bin/python \
  --index "$PYPI_MIRROR" \
  --index-strategy unsafe-best-match \
  "torch==2.11.0" \
  "torchaudio==2.11.0" \
  "torchvision==0.26.0"
"$UV_BIN" pip install \
  --python .venv-tts/bin/python \
  --index "$PYPI_MIRROR" \
  --index-strategy unsafe-best-match \
  "vllm==0.21.0"

if [[ ! -d "$VLLM_OMNI_DIR/.git" ]]; then
  git clone https://github.com/vllm-project/vllm-omni.git "$VLLM_OMNI_DIR"
fi
git -C "$VLLM_OMNI_DIR" fetch --depth 1 origin \
  "refs/tags/$VLLM_OMNI_TAG:refs/tags/$VLLM_OMNI_TAG"
test "$(git -C "$VLLM_OMNI_DIR" rev-list -n 1 "$VLLM_OMNI_TAG")" = \
  "$VLLM_OMNI_COMMIT"
git -C "$VLLM_OMNI_DIR" checkout --detach "$VLLM_OMNI_COMMIT"
"$UV_BIN" pip install \
  --python .venv-tts/bin/python \
  -e "$VLLM_OMNI_DIR"
"$UV_BIN" pip install \
  --python .venv-tts/bin/python \
  -r requirements-tts.txt
.venv-tts/bin/python - <<'PY'
import os

from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="openbmb/VoxCPM2",
    local_dir=f"{os.environ['PROJECT_ROOT']}/models/VoxCPM2",
)
PY

install -m 0644 \
  deploy/systemd-user/ai-centre-asr-gpu0.service \
  "$USER_UNIT_DIR/ai-centre-asr-gpu0.service"
install -m 0644 \
  deploy/systemd-user/ai-centre-voxcpm2-gpu1.service \
  "$USER_UNIT_DIR/ai-centre-voxcpm2-gpu1.service"
install -m 0644 \
  deploy/systemd-user/ai-centre-tts-backend.service \
  "$USER_UNIT_DIR/ai-centre-tts-backend.service"

systemctl --user daemon-reload
systemctl --user enable ai-centre-asr-gpu0.service
systemctl --user enable ai-centre-voxcpm2-gpu1.service
systemctl --user enable ai-centre-tts-backend.service
systemctl --user restart ai-centre-voxcpm2-gpu1.service
systemctl --user restart ai-centre-asr-gpu0.service
systemctl --user restart ai-centre-tts-backend.service
