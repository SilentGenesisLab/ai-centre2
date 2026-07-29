#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


async def run_request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    mode: str,
    url: str,
    request_id: int,
    audio: bytes | None,
) -> dict[str, Any]:
    async with semaphore:
        started = time.perf_counter()
        try:
            if mode == "tts":
                response = await client.post(
                    f"{url.rstrip('/')}/tts",
                    json={
                        "text": f"AI Centre concurrent request {request_id}.",
                        "cfg_value": 2.0,
                        "inference_timesteps": 10,
                    },
                )
            else:
                response = await client.post(
                    f"{url.rstrip('/')}/asr",
                    data={"language": "en", "beam_size": "5"},
                    files={"file": ("probe.wav", audio, "audio/wav")},
                )
            response.raise_for_status()
            return {
                "ok": True,
                "status": response.status_code,
                "elapsed_seconds": time.perf_counter() - started,
                "response_bytes": len(response.content),
            }
        except Exception as exc:
            return {
                "ok": False,
                "elapsed_seconds": time.perf_counter() - started,
                "error": f"{type(exc).__name__}: {exc}",
            }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("asr", "tts"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    if args.concurrency < 1 or args.requests < 1:
        parser.error("--concurrency and --requests must be positive")
    audio = None
    if args.mode == "asr":
        if args.audio is None:
            parser.error("--audio is required for ASR")
        audio = args.audio.read_bytes()

    semaphore = asyncio.Semaphore(args.concurrency)
    timeout = httpx.Timeout(args.timeout, connect=15)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            *(
                run_request(
                    client,
                    semaphore,
                    args.mode,
                    args.url,
                    request_id,
                    audio,
                )
                for request_id in range(args.requests)
            )
        )
    wall_seconds = time.perf_counter() - started
    successes = [item for item in results if item["ok"]]
    latencies = [float(item["elapsed_seconds"]) for item in successes]
    report: dict[str, Any] = {
        "mode": args.mode,
        "url": args.url,
        "concurrency": args.concurrency,
        "requests": args.requests,
        "successes": len(successes),
        "failures": len(results) - len(successes),
        "wall_seconds": round(wall_seconds, 3),
        "requests_per_second": round(len(successes) / wall_seconds, 3),
        "results": results,
    }
    if latencies:
        report["latency_seconds"] = {
            "mean": round(statistics.mean(latencies), 3),
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3),
        }
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if len(successes) == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
