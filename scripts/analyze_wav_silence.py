#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import struct
import wave
from pathlib import Path


def _runs(flags: list[bool], unit_seconds: float) -> list[dict[str, float]]:
    found: list[dict[str, float]] = []
    start: int | None = None
    for index, silent in enumerate(flags + [False]):
        if silent and start is None:
            start = index
        elif not silent and start is not None:
            found.append(
                {
                    "start": round(start * unit_seconds, 6),
                    "end": round(index * unit_seconds, 6),
                    "duration": round((index - start) * unit_seconds, 6),
                }
            )
            start = None
    return found


def analyze(path: Path, frame_ms: int = 20) -> dict[str, object]:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        sample_width = source.getsampwidth()
        frame_count = source.getnframes()
        raw = source.readframes(frame_count)
    if sample_width != 2:
        raise ValueError(f"only PCM16 WAV is supported, got {sample_width * 8}-bit")
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    mono = samples[::channels]
    exact_zero = _runs([sample == 0 for sample in mono], 1.0 / sample_rate)

    frame_samples = max(1, sample_rate * frame_ms // 1000)
    frame_dbfs: list[float] = []
    for offset in range(0, len(mono), frame_samples):
        frame = mono[offset : offset + frame_samples]
        rms = math.sqrt(sum(sample * sample for sample in frame) / max(1, len(frame)))
        frame_dbfs.append(20.0 * math.log10(max(rms, 1e-12) / 32768.0))

    # Some streaming WAV responses leave a sentinel frame count in the RIFF
    # header. The decoded payload length is authoritative for a saved file.
    duration = len(mono) / sample_rate
    result: dict[str, object] = {
        "path": str(path.resolve()),
        "duration_seconds": round(duration, 6),
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": sample_width * 8,
        "frame_ms": frame_ms,
    }
    for label, threshold in (("minus_40_dbfs", -40.0), ("minus_60_dbfs", -60.0)):
        runs = _runs([value <= threshold for value in frame_dbfs], frame_ms / 1000.0)
        result[label] = {
            "longest_seconds": max((run["duration"] for run in runs), default=0.0),
            "runs_at_least_0_25s": [run for run in runs if run["duration"] >= 0.25],
        }
    result["exact_zero"] = {
        "longest_seconds": max((run["duration"] for run in exact_zero), default=0.0),
        "runs_at_least_0_25s": [run for run in exact_zero if run["duration"] >= 0.25],
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="+", type=Path)
    parser.add_argument("--frame-ms", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps([analyze(path, args.frame_ms) for path in args.wav], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
