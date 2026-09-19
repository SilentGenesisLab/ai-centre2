#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/donxu/ai-centre/runtime/validation/audio-separation-20260820
PYTHON=/home/donxu/ai-centre/.venv-tts-v026/bin/python
FFMPEG=/home/donxu/ai-centre/.venv-control/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2
ID=fixture-015

export CUDA_VISIBLE_DEVICES=1
mkdir -p "$ROOT/outputs/$ID" "$ROOT/previews/$ID"

/usr/bin/time -v -o "$ROOT/outputs/$ID/resource.txt" \
  "$PYTHON" "$ROOT/run_bandit_v2_separation.py" \
  --input "$ROOT/inputs/source-audio-v2/$ID.wav" \
  --output "$ROOT/outputs/$ID" \
  --repo "$ROOT/bandit-v2" \
  --checkpoint "$ROOT/models/checkpoint-multi.slim.pt" \
  --batch-size 4 --device cuda \
  > "$ROOT/outputs/$ID/stdout.json"

curl -sS --fail --max-time 600 -X POST http://127.0.0.1:9001/asr \
  -F "file=@$ROOT/inputs/source-audio-v2/$ID.wav" -F beam_size=5 \
  > "$ROOT/asr/$ID-mix.json"
curl -sS --fail --max-time 600 -X POST http://127.0.0.1:9001/asr \
  -F "file=@$ROOT/outputs/$ID/speech.wav" -F beam_size=5 \
  > "$ROOT/asr/$ID-speech.json"
curl -sS --fail --max-time 600 -X POST http://127.0.0.1:9001/asr \
  -F "file=@$ROOT/outputs/$ID/background.wav" -F beam_size=5 \
  > "$ROOT/asr/$ID-background.json"

"$FFMPEG" -hide_banner -loglevel error -y \
  -i "$ROOT/inputs/source-audio-v2/$ID.wav" -c:a aac -b:a 160k -movflags +faststart \
  "$ROOT/previews/$ID/mix.m4a"

for stem in speech music sfx background; do
  "$FFMPEG" -hide_banner -loglevel error -y \
    -i "$ROOT/outputs/$ID/$stem.wav" -c:a aac -b:a 160k -movflags +faststart \
    "$ROOT/previews/$ID/$stem.m4a"
done

cat "$ROOT/outputs/$ID/metrics.json"
