#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/donxu/ai-centre/runtime/validation/audio-separation-20260820
PYTHON=/home/donxu/ai-centre/.venv-tts-v026/bin/python
SEPARATOR="$ROOT/roformer-deps/bin/audio-separator"
MODEL=UVR-MDX-NET-Inst_HQ_5.onnx
FFMPEG=/home/donxu/ai-centre/.venv-control/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2

export PYTHONPATH="$ROOT/roformer-deps"
export PATH="$ROOT/bin:/usr/local/bin:/usr/bin:/bin"
export CUDA_VISIBLE_DEVICES=1

for fixture_id in fixture-003 fixture-006 fixture-013 fixture-014 fixture-015; do
  output_dir="$ROOT/mdx-ab/$fixture_id"
  input="$ROOT/inputs/source-audio-v2/$fixture_id.wav"
  mkdir -p "$output_dir"

  if ! compgen -G "$output_dir/*\(Vocals\)*.wav" >/dev/null; then
    /usr/bin/time -v -o "$output_dir/resource.txt" \
      "$PYTHON" "$SEPARATOR" \
      -m "$MODEL" \
      --model_file_dir "$ROOT/models/mdx" \
      --output_dir "$output_dir" \
      --output_format WAV \
      --sample_rate 48000 \
      --use_soundfile \
      --mdx_batch_size 1 \
      "$input" >"$output_dir/stdout.log" 2>"$output_dir/stderr.log"
  fi

  vocals=$(find "$output_dir" -maxdepth 1 -type f -name '*\(Vocals\)*.wav' -print -quit)
  instrumental=$(find "$output_dir" -maxdepth 1 -type f -name '*\(Instrumental\)*.wav' -print -quit)
  test -n "$vocals"
  test -n "$instrumental"

  "$FFMPEG" -hide_banner -loglevel error -y -i "$vocals" \
    -c:a aac -b:a 160k -movflags +faststart "$output_dir/vocals.m4a"
  "$FFMPEG" -hide_banner -loglevel error -y -i "$instrumental" \
    -c:a aac -b:a 160k -movflags +faststart "$output_dir/instrumental.m4a"

  curl -sS --fail --max-time 600 -X POST http://127.0.0.1:9001/asr \
    -F "file=@$vocals" -F beam_size=5 >"$output_dir/asr-vocals.json"
  curl -sS --fail --max-time 600 -X POST http://127.0.0.1:9001/asr \
    -F "file=@$instrumental" -F beam_size=5 >"$output_dir/asr-instrumental.json"

  "$PYTHON" - "$input" "$vocals" "$instrumental" "$output_dir/metrics.json" <<'PY'
import json
import math
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


def read(path: str) -> tuple[np.ndarray, int]:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    return audio, sample_rate


def stats(audio: np.ndarray) -> dict[str, float]:
    return {
        "rms_dbfs": float(20 * np.log10(np.sqrt(np.mean(audio ** 2)) + 1e-12)),
        "peak_dbfs": float(20 * np.log10(np.max(np.abs(audio)) + 1e-12)),
        "clipped_fraction": float(np.mean(np.abs(audio) >= 1.0)),
    }


mix, sample_rate = read(sys.argv[1])
vocals, vocals_rate = read(sys.argv[2])
instrumental, instrumental_rate = read(sys.argv[3])
if sample_rate != vocals_rate or sample_rate != instrumental_rate:
    raise SystemExit("sample-rate mismatch")
samples = min(len(mix), len(vocals), len(instrumental))
mix = mix[:samples]
vocals = vocals[:samples]
instrumental = instrumental[:samples]
error = mix - vocals - instrumental
result = {
    "backend": "onnxruntime-cpu",
    "model": "UVR-MDX-NET Inst HQ 5",
    "duration_seconds": samples / sample_rate,
    "sample_rate": sample_rate,
    "channels": mix.shape[1],
    "reconstruction_snr_db": float(10 * np.log10((np.mean(mix ** 2) + 1e-20) / (np.mean(error ** 2) + 1e-20))),
    "mix": stats(mix),
    "vocals": stats(vocals),
    "instrumental": stats(instrumental),
}
Path(sys.argv[4]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
PY
done
