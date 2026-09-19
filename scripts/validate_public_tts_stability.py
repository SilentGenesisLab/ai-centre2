#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import statistics
import time
import wave
from pathlib import Path
from typing import Any

import httpx


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def wav_duration(audio: bytes) -> tuple[float, bool]:
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            duration = source.getnframes() / source.getframerate()
        finalized = (
            int.from_bytes(audio[4:8], "little") == len(audio) - 8
            and int.from_bytes(audio[40:44], "little") == len(audio) - 44
        )
        return duration, finalized
    except (EOFError, wave.Error, ZeroDivisionError):
        return 0.0, False


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--reference-url")
    parser.add_argument(
        "--text",
        default="欢迎体验我们的新产品，它将为您带来更加简单和温暖的使用体验。",
    )
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("SERVICE_TOKEN")
    if not token:
        raise SystemExit("SERVICE_TOKEN environment variable is required")

    payload: dict[str, Any] = {
        "text": args.text,
        "language": "auto",
        "quality_mode": "standard",
        "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0},
    }
    if args.reference_url:
        payload.update(
            {
                "emotion": "真诚、温暖、有感染力，重点内容适当加强",
                "reference_audio_url": args.reference_url,
                "prompt_text": "",
                "emotion_enhance": True,
            }
        )
    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=max(4, args.concurrency),
        max_keepalive_connections=max(2, args.concurrency),
    )
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(900, connect=15),
        limits=limits,
        headers={"authorization": f"Bearer {token}"},
    ) as client:
        async def run(index: int) -> dict[str, Any]:
            async with semaphore:
                started = time.perf_counter()
                response = await client.post(
                    f"{args.base_url.rstrip('/')}/v2/tts/speech",
                    json=payload,
                )
                elapsed = time.perf_counter() - started
            duration, riff_ok = wav_duration(response.content)
            headers = response.headers
            return {
                "index": index,
                "http_status": response.status_code,
                "request_seconds": round(elapsed, 4),
                "duration_seconds": round(duration, 4),
                "riff_finalized": riff_ok,
                "quality_passed": headers.get("x-tts-quality-passed") == "true",
                "quality_attempts": int(headers.get("x-tts-quality-attempts", "0")),
                "content_cer": float(headers.get("x-tts-content-cer", "1")),
                "unexpected_prefix": headers.get("x-tts-unexpected-prefix") == "true",
                "unexpected_suffix": headers.get("x-tts-unexpected-suffix") == "true",
                "tail_trimmed": headers.get("x-tts-tail-trimmed") == "true",
                "degraded_reason": headers.get("x-tts-degraded-reason", "missing"),
            }

        results = await asyncio.gather(*(run(index) for index in range(args.repeats)))

    latencies = [float(item["request_seconds"]) for item in results]
    durations = [float(item["duration_seconds"]) for item in results if item["duration_seconds"]]
    median_duration = statistics.median(durations) if durations else 0.0
    successful = [item for item in results if item["http_status"] == 200]
    exact = [item for item in successful if item["content_cer"] == 0.0]
    extra = [
        item
        for item in successful
        if item["unexpected_prefix"] or item["unexpected_suffix"]
    ]
    report = {
        "passed": bool(
            len(successful) == args.repeats
            and len(exact) / max(1, args.repeats) >= 0.98
            and len(extra) <= 1
            and all(item["riff_finalized"] for item in successful)
            and percentile(durations, 0.99) / max(0.001, median_duration) <= 1.8
        ),
        "requests": args.repeats,
        "concurrency": args.concurrency,
        "http_successes": len(successful),
        "quality_passes": sum(bool(item["quality_passed"]) for item in results),
        "content_exact_count": len(exact),
        "extra_speech_count": len(extra),
        "tail_trimmed_count": sum(bool(item["tail_trimmed"]) for item in results),
        "single_attempt_count": sum(item["quality_attempts"] == 1 for item in results),
        "p50_request_seconds": round(percentile(latencies, 0.50), 4),
        "p95_request_seconds": round(percentile(latencies, 0.95), 4),
        "p99_request_seconds": round(percentile(latencies, 0.99), 4),
        "median_duration_seconds": round(median_duration, 4),
        "p99_duration_seconds": round(percentile(durations, 0.99), 4),
        "p99_duration_ratio": round(
            percentile(durations, 0.99) / max(0.001, median_duration), 4
        ),
        "slow_request_count": sum(value > 4.0 for value in latencies),
        "latency_samples_seconds": [
            item["request_seconds"] for item in sorted(
                results,
                key=lambda item: item["index"],
            )
        ],
        "failures": [
            item
            for item in results
            if item["http_status"] != 200
            or not item["quality_passed"]
            or item["content_cer"] != 0.0
            or item["unexpected_prefix"]
            or item["unexpected_suffix"]
            or not item["riff_finalized"]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
