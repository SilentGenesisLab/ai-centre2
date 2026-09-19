#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
MUSETALK_ROOT="${MUSETALK_ROOT:-/home/donxu/services/MuseTalk}"
UV_BIN="${UV_BIN:-/home/donxu/.local/bin/uv}"
GFPGAN_MODEL_PATH="${GFPGAN_MODEL_PATH:-/home/donxu/services/GFPGAN/GFPGANv1.3.pth}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

test -d "$PROJECT_ROOT"
test -d "$MUSETALK_ROOT"
test -x "$UV_BIN"

cd "$MUSETALK_ROOT"
if [[ ! -x .venv/bin/python ]]; then
  "$UV_BIN" venv --python 3.12 .venv
fi

"$UV_BIN" pip install --python .venv/bin/python \
  pip cython numpy==1.26.4 setuptools
"$UV_BIN" pip install --python .venv/bin/python \
  chumpy==0.70 xtcocotools==1.14.3 \
  --no-build-isolation
"$UV_BIN" pip install --python .venv/bin/python \
  -r "$PROJECT_ROOT/requirements-musetalk.txt"
.venv/bin/python "$PROJECT_ROOT/scripts/patch_mmdet_for_musetalk.py"

mkdir -p ffmpeg
ffmpeg_bin="$(.venv/bin/python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
ln -sfn "$ffmpeg_bin" ffmpeg/ffmpeg

required_models=(
  models/musetalkV15/unet.pth
  models/musetalkV15/musetalk.json
  models/dwpose/dw-ll_ucoco_384.pth
  models/face-parse-bisent/79999_iter.pth
  models/face-parse-bisent/resnet18-5c106cde.pth
  models/sd-vae/config.json
  models/sd-vae/diffusion_pytorch_model.bin
  models/whisper/config.json
  models/whisper/pytorch_model.bin
  models/whisper/preprocessor_config.json
  gfpgan/weights/detection_Resnet50_Final.pth
  gfpgan/weights/parsing_parsenet.pth
  models/torch-cache/hub/checkpoints/s3fd-619a316812.pth
)
for model in "${required_models[@]}"; do
  test -s "$model" || {
    echo "Missing model artifact: $MUSETALK_ROOT/$model" >&2
    exit 1
  }
done
test -s "$GFPGAN_MODEL_PATH" || {
  echo "Missing GFPGAN model artifact: $GFPGAN_MODEL_PATH" >&2
  exit 1
}

mkdir -p "$PROJECT_ROOT/runtime/musetalk/jobs" "$USER_UNIT_DIR"
install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-musetalk.service" \
  "$USER_UNIT_DIR/ai-centre-musetalk.service"

systemctl --user daemon-reload
systemctl --user enable --now ai-centre-musetalk.service
systemctl --user restart ai-centre-control.service
