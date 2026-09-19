#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
SERVICE_ROOT="${DEPTH_SOURCE_DIR:-/home/donxu/services/video-depth-anything-small}"
VENV="${DEPTH_VENV:-$PROJECT_ROOT/.venv-depth}"
PYTHON_BIN="${DEPTH_PYTHON_BIN:-$PROJECT_ROOT/.venv-control/bin/python}"
COMMIT="4f5ae23172ba60fd7bc11ef671cca678842c7072"
RAW_BASE="https://raw.githubusercontent.com/DepthAnything/Video-Depth-Anything/$COMMIT"

FILES=(
  LICENSE
  utils/__init__.py
  utils/util.py
  video_depth_anything/__init__.py
  video_depth_anything/video_depth.py
  video_depth_anything/dinov2.py
  video_depth_anything/dpt.py
  video_depth_anything/dpt_temporal.py
  video_depth_anything/dinov2_layers/__init__.py
  video_depth_anything/dinov2_layers/attention.py
  video_depth_anything/dinov2_layers/block.py
  video_depth_anything/dinov2_layers/drop_path.py
  video_depth_anything/dinov2_layers/layer_scale.py
  video_depth_anything/dinov2_layers/mlp.py
  video_depth_anything/dinov2_layers/patch_embed.py
  video_depth_anything/dinov2_layers/swiglu_ffn.py
  video_depth_anything/motion_module/__init__.py
  video_depth_anything/motion_module/attention.py
  video_depth_anything/motion_module/motion_module.py
  video_depth_anything/util/__init__.py
  video_depth_anything/util/blocks.py
  video_depth_anything/util/transform.py
)

mkdir -p "$SERVICE_ROOT/checkpoints"
for relative in "${FILES[@]}"; do
  mkdir -p "$SERVICE_ROOT/$(dirname "$relative")"
  if [[ "$relative" == "utils/__init__.py" \
     || "$relative" == "video_depth_anything/__init__.py" \
     || "$relative" == "video_depth_anything/motion_module/__init__.py" \
     || "$relative" == "video_depth_anything/util/__init__.py" ]]; then
    touch "$SERVICE_ROOT/$relative"
    continue
  fi
  if [[ -s "$SERVICE_ROOT/$relative" ]]; then
    continue
  fi
  curl -fsSLo "$SERVICE_ROOT/$relative" "$RAW_BASE/$relative"
done

CHECKPOINT="$SERVICE_ROOT/checkpoints/video_depth_anything_vits.pth"
if [[ ! -s "$CHECKPOINT" ]]; then
  curl -fL --retry 3 --retry-delay 3 \
    -o "$CHECKPOINT.part" \
    "https://huggingface.co/depth-anything/Video-Depth-Anything-Small/resolve/main/video_depth_anything_vits.pth"
  mv "$CHECKPOINT.part" "$CHECKPOINT"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip wheel
"$VENV/bin/python" -m pip install \
  --index-url https://download.pytorch.org/whl/cu128 \
  torch==2.8.0 torchvision==0.23.0
"$VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-depth.txt"

mkdir -p /home/donxu/.config/systemd/user "$PROJECT_ROOT/runtime/video-depth"
install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-depth-worker.service" \
  /home/donxu/.config/systemd/user/ai-centre-depth-worker.service
systemctl --user daemon-reload
systemctl --user enable --now ai-centre-depth-worker.service
