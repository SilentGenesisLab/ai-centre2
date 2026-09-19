#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from validate_voxcpm2_quality import (
    edit_distance,
    normalized,
    transcribe,
    wav_duration,
)


TEXTS = [
    "并发质量验证请求一，系统应当完整读出这句话。",
    "并发质量验证请求二，请不要混入其他请求内容。",
    "并发质量验证请求三，现在检查语音是否清晰。",
    "并发质量验证请求四，现在检查结尾是否完整。",
]


def generate(
    api_base: str,
    model: str,
    output_dir: Path,
    index: int,
    reference_data_uri: str,
    reference_text: str,
) -> dict[str, Any]:
    target = TEXTS[index % len(TEXTS)]
    started = time.perf_counter()
    with httpx.Client(timeout=httpx.Timeout(900, connect=15)) as client:
        response = client.post(
            f"{api_base.rstrip('/')}/v1/audio/speech",
            json={
                "model": model,
                "input": target,
                "voice": "default",
                "response_format": "wav",
                "ref_audio": reference_data_uri,
                "ref_text": reference_text,
            },
        )
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    output = output_dir / f"concurrent-{index + 1}.wav"
    output.write_bytes(response.content)
    return {
        "index": index,
        "target_text": target,
        "request_seconds": round(elapsed, 3),
        "output": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8194")
    parser.add_argument("--asr-base", default="http://127.0.0.1:9001")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2, choices=(1, 2, 4))
    parser.add_argument("--requests", type=int, default=4)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runtime/validation/voxcpm2-v026-concurrency"),
    )
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(900, connect=15)
    with httpx.Client(timeout=timeout) as client:
        models_response = client.get(f"{args.api_base.rstrip('/')}/v1/models")
        models_response.raise_for_status()
        model = models_response.json()["data"][0]["id"]
        reference_text = transcribe(client, args.asr_base, args.reference)

    reference_data_uri = "data:audio/wav;base64," + base64.b64encode(
        args.reference.read_bytes()
    ).decode("ascii")
    generated: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                generate,
                args.api_base,
                model,
                args.output_dir,
                index,
                reference_data_uri,
                reference_text,
            )
            for index in range(args.requests)
        ]
        for future in as_completed(futures):
            generated.append(future.result())

    results = []
    with httpx.Client(timeout=timeout) as client:
        for item in sorted(generated, key=lambda value: value["index"]):
            transcript = transcribe(client, args.asr_base, item["output"])
            expected = normalized(item["target_text"])
            actual = normalized(transcript)
            error_rate = edit_distance(expected, actual) / max(1, len(expected))
            duration = wav_duration(item["output"])
            results.append(
                {
                    **item,
                    "output": str(item["output"]),
                    "asr_text": transcript,
                    "character_error_rate": round(error_rate, 4),
                    "duration_seconds": round(duration, 3),
                    "passed": error_rate <= 0.35
                    and 0.5 <= duration <= max(12.0, len(expected) * 0.75),
                }
            )

    report = {
        "passed": all(result["passed"] for result in results),
        "model": model,
        "concurrency": args.concurrency,
        "requests": args.requests,
        "results": results,
    }
    report_path = args.output_dir / f"report-c{args.concurrency}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
