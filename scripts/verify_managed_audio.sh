#!/usr/bin/env bash
set -euo pipefail

ASR_URL="${ASR_URL:-http://127.0.0.1:9001}"
TTS_URL="${TTS_URL:-http://127.0.0.1:8193}"
PROBE_DIR="${PROBE_DIR:-/tmp/ai-centre-audio-verify}"

mkdir -p "$PROBE_DIR"

curl -fsS "$ASR_URL/health" | python3 -m json.tool
curl -fsS "$TTS_URL/health" | python3 -m json.tool

curl -fsS \
  -X POST "$TTS_URL/tts" \
  -H "Content-Type: application/json" \
  -d '{"text":"AI Centre managed audio verification.","cfg_value":2.0,"inference_timesteps":10}' \
  --output "$PROBE_DIR/tts.wav"

test -s "$PROBE_DIR/tts.wav"
file "$PROBE_DIR/tts.wav"

curl -fsS \
  -X POST "$ASR_URL/asr" \
  -F "file=@$PROBE_DIR/tts.wav" \
  -F "language=en" \
  -F "beam_size=5" \
  | python3 -m json.tool

systemctl --user --no-pager --full status \
  ai-centre-asr-gpu0.service \
  ai-centre-voxcpm2-gpu1.service \
  ai-centre-tts-backend.service
