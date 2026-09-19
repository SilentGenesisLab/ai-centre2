#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import time
import wave
from pathlib import Path
from typing import Any

import httpx


def normalized(text: str) -> str:
    return "".join(re.findall(r"[\w\u3400-\u9fff]", text.lower()))


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def transcribe(client: httpx.Client, asr_base: str, audio: Path) -> str:
    with audio.open("rb") as stream:
        response = client.post(
            f"{asr_base.rstrip('/')}/asr",
            data={"beam_size": "5"},
            files={"file": (audio.name, stream, "audio/wav")},
        )
    response.raise_for_status()
    return "".join(segment["text"] for segment in response.json()["segments"]).strip()


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        return wav_file.getnframes() / wav_file.getframerate()


def run_case(
    client: httpx.Client,
    api_base: str,
    asr_base: str,
    model: str,
    output_dir: Path,
    name: str,
    text: str,
    reference_data_uri: str | None = None,
    reference_text: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "voice": "default",
        "response_format": "wav",
    }
    if reference_data_uri:
        payload["ref_audio"] = reference_data_uri
        payload["ref_text"] = reference_text

    started = time.perf_counter()
    response = client.post(f"{api_base.rstrip('/')}/v1/audio/speech", json=payload)
    elapsed = time.perf_counter() - started
    response.raise_for_status()

    output = output_dir / f"{name}.wav"
    output.write_bytes(response.content)
    transcript = transcribe(client, asr_base, output)
    expected = normalized(text)
    actual = normalized(transcript)
    distance = edit_distance(expected, actual)
    character_error_rate = distance / max(1, len(expected))
    duration = wav_duration(output)
    max_duration = max(12.0, len(expected) * 0.75)
    passed = character_error_rate <= 0.35 and 0.5 <= duration <= max_duration
    return {
        "name": name,
        "passed": passed,
        "target_text": text,
        "asr_text": transcript,
        "character_error_rate": round(character_error_rate, 4),
        "duration_seconds": round(duration, 3),
        "request_seconds": round(elapsed, 3),
        "output": str(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8194")
    parser.add_argument("--asr-base", default="http://127.0.0.1:9001")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runtime/validation/voxcpm2-v026"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    reference_bytes = args.reference.read_bytes()
    reference_data_uri = "data:audio/wav;base64," + base64.b64encode(
        reference_bytes
    ).decode("ascii")
    timeout = httpx.Timeout(900, connect=15)
    with httpx.Client(timeout=timeout) as client:
        models_response = client.get(f"{args.api_base.rstrip('/')}/v1/models")
        models_response.raise_for_status()
        model = models_response.json()["data"][0]["id"]
        reference_text = transcribe(client, args.asr_base, args.reference)
        results = [
            run_case(
                client,
                args.api_base,
                args.asr_base,
                model,
                args.output_dir,
                "plain_chinese",
                "今天阳光很好，我们正在验证新版语音合成服务。",
            ),
            run_case(
                client,
                args.api_base,
                args.asr_base,
                model,
                args.output_dir,
                "cloned_chinese",
                "这是一段新版深度语音克隆质量验证音频。",
                reference_data_uri,
                reference_text,
            ),
        ]

    report = {
        "passed": all(result["passed"] for result in results),
        "model": model,
        "reference": str(args.reference),
        "reference_text": reference_text,
        "results": results,
    }
    report_path = args.output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
