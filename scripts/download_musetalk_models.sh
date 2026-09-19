#!/usr/bin/env bash
set -euo pipefail

MUSETALK_ROOT="${MUSETALK_ROOT:-/home/donxu/services/MuseTalk}"
PYTHON_BIN="${MUSETALK_PYTHON:-$MUSETALK_ROOT/.venv/bin/python}"
GFPGAN_ROOT="${GFPGAN_ROOT:-/home/donxu/services/GFPGAN}"

test -d "$MUSETALK_ROOT"
test -x "$PYTHON_BIN"

cd "$MUSETALK_ROOT"
mkdir -p \
  models/musetalkV15 \
  models/dwpose \
  models/face-parse-bisent \
  models/sd-vae \
  models/whisper
mkdir -p "$GFPGAN_ROOT"
mkdir -p gfpgan/weights
mkdir -p models/torch-cache/hub/checkpoints

"${PYTHON_BIN%/python}/huggingface-cli" download TMElyralab/MuseTalk \
  --local-dir models \
  --include musetalkV15/musetalk.json musetalkV15/unet.pth
"${PYTHON_BIN%/python}/huggingface-cli" download stabilityai/sd-vae-ft-mse \
  --local-dir models/sd-vae \
  --include config.json diffusion_pytorch_model.bin
"${PYTHON_BIN%/python}/huggingface-cli" download openai/whisper-tiny \
  --local-dir models/whisper \
  --include config.json pytorch_model.bin preprocessor_config.json
"${PYTHON_BIN%/python}/huggingface-cli" download yzd-v/DWPose \
  --local-dir models/dwpose \
  --include dw-ll_ucoco_384.pth
"${PYTHON_BIN%/python}/huggingface-cli" download \
  ManyOtherFunctions/face-parse-bisent \
  --local-dir models/face-parse-bisent \
  --include 79999_iter.pth resnet18-5c106cde.pth
"${PYTHON_BIN%/python}/huggingface-cli" download nlightcho/gfpgan-v1.3 \
  --local-dir "$GFPGAN_ROOT" \
  --include GFPGANv1.3.pth
"${PYTHON_BIN%/python}/huggingface-cli" download gmk123/GFPGAN \
  --local-dir gfpgan/weights \
  --include detection_Resnet50_Final.pth parsing_parsenet.pth
"${PYTHON_BIN%/python}/huggingface-cli" download chunyu-li/face-alignment \
  --local-dir models/torch-cache/hub/checkpoints \
  --include s3fd-619a316812.pth

echo "MuseTalk and GFPGAN production model set downloaded."
