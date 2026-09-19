#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=/home/donxu/ai-centre
SERVICE_ROOT=/home/donxu/services/audio-separation-bandit
VALIDATION_ROOT=/home/donxu/ai-centre/runtime/validation/audio-separation-20260820
SOURCE_COMMIT=d5563d9031e95fdaa3e5a73d5020b9a0df61adb6
CHECKPOINT_SHA256=ba9ba16504cd5d987a8c01a00307afba1340f251c18c70406c787bf76e2c4102
ZENODO_CHECKPOINT_URL=https://zenodo.org/api/records/12701995/files/checkpoint-multi.ckpt/content
CONTROL_PYTHON=/home/donxu/ai-centre/.venv-control/bin/python
INFERENCE_PYTHON=/home/donxu/ai-centre/.venv-tts-v026/bin/python

mkdir -p "$SERVICE_ROOT"

if [ ! -f "$SERVICE_ROOT/src/models/bandit/bandit.py" ]; then
  if [ -f "$VALIDATION_ROOT/bandit-v2/src/models/bandit/bandit.py" ]; then
    cp -a "$VALIDATION_ROOT/bandit-v2/src" "$SERVICE_ROOT/"
    cp "$VALIDATION_ROOT/bandit-v2/LICENSE" "$SERVICE_ROOT/LICENSE"
    cp "$VALIDATION_ROOT/bandit-v2/README.md" "$SERVICE_ROOT/README.upstream.md"
  else
    temporary=$(mktemp -d)
    trap 'rm -rf "$temporary"' EXIT
    curl -fsSL --retry 3 \
      "https://github.com/kwatcharasupat/bandit-v2/archive/$SOURCE_COMMIT.tar.gz" \
      -o "$temporary/bandit-v2.tar.gz"
    tar -xzf "$temporary/bandit-v2.tar.gz" -C "$temporary"
    cp -a "$temporary/bandit-v2-$SOURCE_COMMIT/src" "$SERVICE_ROOT/"
    cp "$temporary/bandit-v2-$SOURCE_COMMIT/LICENSE" "$SERVICE_ROOT/LICENSE"
    cp "$temporary/bandit-v2-$SOURCE_COMMIT/README.md" "$SERVICE_ROOT/README.upstream.md"
  fi
fi

if [ ! -f "$SERVICE_ROOT/checkpoint-multi.slim.pt" ]; then
  if [ -f "$VALIDATION_ROOT/models/checkpoint-multi.slim.pt" ]; then
    cp "$VALIDATION_ROOT/models/checkpoint-multi.slim.pt" \
      "$SERVICE_ROOT/checkpoint-multi.slim.pt"
  else
    curl -fsSL --retry 3 "$ZENODO_CHECKPOINT_URL" \
      -o "$SERVICE_ROOT/checkpoint-multi.slim.pt.part"
    mv "$SERVICE_ROOT/checkpoint-multi.slim.pt.part" \
      "$SERVICE_ROOT/checkpoint-multi.slim.pt"
  fi
fi

if [ "$(stat -c%s "$SERVICE_ROOT/checkpoint-multi.slim.pt")" -lt 200000000 ]; then
  echo "$CHECKPOINT_SHA256  $SERVICE_ROOT/checkpoint-multi.slim.pt" | sha256sum -c -
fi

PYTHONPATH="$PROJECT_ROOT" "$CONTROL_PYTHON" - <<'PY'
import celery
import redis

import control_plane.audio_separation_tasks

print("audio separation worker dependencies:", celery.__version__, redis.__version__)
PY

PYTHONPATH="$PROJECT_ROOT" "$INFERENCE_PYTHON" - <<'PY'
import librosa
import numpy
import soundfile
import torch

from control_plane.audio_separation_inference import _load_model

print("audio separation inference dependencies:", torch.__version__)
PY

install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-audio-separation-worker.service" \
  "$HOME/.config/systemd/user/ai-centre-audio-separation-worker.service"
systemctl --user daemon-reload
systemctl --user enable --now ai-centre-audio-separation-worker.service
systemctl --user is-active ai-centre-audio-separation-worker.service
