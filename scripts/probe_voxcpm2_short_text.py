#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import math
import wave
from array import array
from pathlib import Path

import httpx


def audio_metrics(audio: bytes) -> dict[str, float | int]:
    with wave.open(io.BytesIO(audio), "rb") as source:
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
        duration = source.getnframes() / max(1, rate)
    samples = array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))
    peak = max((abs(sample) for sample in samples), default=0)
    return {
        "duration_seconds": round(duration, 4),
        "rms_dbfs": round(20 * math.log10(max(rms / 32768.0, 1e-8)), 2),
        "peak_dbfs": round(20 * math.log10(max(peak / 32768.0, 1e-8)), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="お帰りなさい。")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--first-seed", type=int, default=40)
    parser.add_argument("--last-seed", type=int, default=55)
    parser.add_argument("--tts-url", default="http://127.0.0.1:8193")
    parser.add_argument("--asr-url", default="http://127.0.0.1:9001")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as client:
        model_response = client.get(args.tts_url.rstrip("/") + "/v1/models")
        model_response.raise_for_status()
        model = model_response.json()["data"][0]["id"]
        for seed in range(args.first_seed, args.last_seed + 1):
            response = client.post(
                args.tts_url.rstrip("/") + "/v1/audio/speech",
                json={
                    "model": model,
                    "input": args.text,
                    "voice": "default",
                    "response_format": "wav",
                    "seed": seed,
                },
            )
            row: dict[str, object] = {"seed": seed, "http_status": response.status_code}
            if response.is_success:
                path = args.output / f"seed-{seed}.wav"
                path.write_bytes(response.content)
                row.update(audio_metrics(response.content))
                asr = client.post(
                    args.asr_url.rstrip("/") + "/asr",
                    data={"language": args.language, "beam_size": "5"},
                    files={"file": (path.name, response.content, "audio/wav")},
                )
                row["asr_status"] = asr.status_code
                if asr.is_success:
                    body = asr.json()
                    row["transcript"] = "".join(
                        str(segment.get("text") or "")
                        for segment in body.get("segments") or []
                    ).strip()
                else:
                    row["transcript"] = ""
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    (args.output / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
