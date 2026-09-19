#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import statistics
import time
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx


def normalized(text: str) -> str:
    digit_equivalents = str.maketrans("零〇一二三四五六七八九", "00123456789")
    return "".join(
        character.lower()
        for character in text.translate(digit_equivalents)
        if character.isalnum()
    )


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def data_uri(path: Path) -> str:
    return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()


def wav_metrics(audio: bytes, last_word_end: float | None) -> dict[str, Any]:
    with wave.open(io.BytesIO(audio), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        width = source.getsampwidth()
        frame_count = source.getnframes()
        frames = source.readframes(frame_count)
    duration = frame_count / rate
    riff_ok = (
        audio[:4] == b"RIFF"
        and int.from_bytes(audio[4:8], "little") == len(audio) - 8
        and int.from_bytes(audio[40:44], "little") == len(audio) - 44
    )
    tail_seconds = max(0.0, duration - last_word_end) if last_word_end else 0.0
    tail_dbfs: float | None = None
    if width == 2 and tail_seconds >= 0.25 and last_word_end is not None:
        samples = array("h")
        samples.frombytes(frames)
        start = min(len(samples), round(last_word_end * rate) * channels)
        tail = samples[start:]
        if tail:
            rms = math.sqrt(sum(sample * sample for sample in tail) / len(tail))
            tail_dbfs = 20 * math.log10(max(rms / 32768.0, 1e-8))
    return {
        "duration_seconds": round(duration, 4),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "riff_finalized": riff_ok,
        "tail_seconds": round(tail_seconds, 4),
        "tail_rms_dbfs": round(tail_dbfs, 2) if tail_dbfs is not None else None,
        "energetic_tail": bool(
            tail_seconds > 0.5
            and tail_dbfs is not None
            and tail_dbfs > -45.0
        ),
    }


def transcribe(client: httpx.Client, asr_base: str, audio: bytes) -> dict[str, Any]:
    response = client.post(
        f"{asr_base.rstrip('/')}/asr",
        data={"language": "zh", "beam_size": "5"},
        files={"file": ("generated.wav", audio, "audio/wav")},
    )
    response.raise_for_status()
    body = response.json()
    words = [
        word
        for segment in body.get("segments") or []
        for word in segment.get("words") or []
    ]
    body["joined_text"] = "".join(
        str(segment.get("text") or "") for segment in body.get("segments") or []
    ).strip()
    body["last_word_end"] = (
        float(words[-1]["end"])
        if words and isinstance(words[-1].get("end"), int | float)
        else None
    )
    body["longest_word_seconds"] = max(
        (
            float(word["end"]) - float(word["start"])
            for word in words
            if isinstance(word.get("start"), int | float)
            and isinstance(word.get("end"), int | float)
        ),
        default=None,
    )
    return body


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def run_one(
    api_base: str,
    asr_base: str,
    model: str,
    case: dict[str, Any],
    seed: int,
    timeout: float,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": case["text"],
        "voice": "default",
        "response_format": "wav",
        "seed": seed,
    }
    if case.get("reference_uri"):
        payload["ref_audio"] = case["reference_uri"]
    if case.get("reference_text"):
        payload["ref_text"] = case["reference_text"]
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15)) as client:
        started = time.perf_counter()
        response = client.post(f"{api_base.rstrip('/')}/v1/audio/speech", json=payload)
        request_seconds = time.perf_counter() - started
        if not response.is_success:
            return {
                "seed": seed,
                "http_status": response.status_code,
                "request_seconds": round(request_seconds, 4),
                "transcript": "",
                "edits": len(normalized(case["text"])),
                "cer": 1.0,
                "content_exact": False,
                "unexpected_prefix": False,
                "unexpected_suffix": False,
                "longest_word_seconds": None,
                "sha256": "",
                "duration_seconds": 0.0,
                "sample_rate": 0,
                "channels": 0,
                "sample_width": 0,
                "riff_finalized": False,
                "tail_seconds": 0.0,
                "tail_rms_dbfs": None,
                "energetic_tail": False,
                "error": "upstream-no-audio",
            }
        transcription = transcribe(client, asr_base, response.content)
    expected = normalized(case["text"])
    actual = normalized(transcription["joined_text"])
    edits = edit_distance(expected, actual)
    media = wav_metrics(response.content, transcription["last_word_end"])
    return {
        "seed": seed,
        "request_seconds": round(request_seconds, 4),
        "transcript": transcription["joined_text"],
        "edits": edits,
        "cer": round(edits / max(1, len(expected)), 6),
        "content_exact": actual == expected,
        "unexpected_prefix": bool(actual and not actual.startswith(expected[:1])),
        "unexpected_suffix": bool(actual.startswith(expected) and len(actual) > len(expected)),
        "longest_word_seconds": transcription["longest_word_seconds"],
        "sha256": hashlib.sha256(response.content).hexdigest(),
        **media,
    }


def summarize(name: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(result["duration_seconds"]) for result in results]
    latencies = [float(result["request_seconds"]) for result in results]
    median_duration = statistics.median(durations)
    exact_count = sum(bool(result["content_exact"]) for result in results)
    extra_count = sum(
        bool(result["unexpected_prefix"])
        or bool(result["unexpected_suffix"])
        or bool(result["energetic_tail"])
        for result in results
    )
    return {
        "name": name,
        "requests": len(results),
        "content_exact_count": exact_count,
        "content_exact_rate": round(exact_count / max(1, len(results)), 4),
        "extra_speech_count": extra_count,
        "riff_pass_count": sum(bool(result["riff_finalized"]) for result in results),
        "unique_audio_count": len({result["sha256"] for result in results}),
        "median_duration_seconds": round(median_duration, 4),
        "p99_duration_seconds": round(percentile(durations, 0.99), 4),
        "p99_duration_ratio": round(
            percentile(durations, 0.99) / max(0.001, median_duration), 4
        ),
        "p50_request_seconds": round(percentile(latencies, 0.50), 4),
        "p95_request_seconds": round(percentile(latencies, 0.95), 4),
        "p99_request_seconds": round(percentile(latencies, 0.99), 4),
        "failures": [
            result
            for result in results
            if not result["content_exact"]
            or result["unexpected_prefix"]
            or result["unexpected_suffix"]
            or result["energetic_tail"]
            or not result["riff_finalized"]
        ][:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8194")
    parser.add_argument("--asr-base", default="http://127.0.0.1:9001")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--same-language-reference", type=Path)
    parser.add_argument("--same-language-text")
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with httpx.Client(timeout=30) as client:
        models = client.get(f"{args.api_base.rstrip('/')}/v1/models")
        models.raise_for_status()
        model = models.json()["data"][0]["id"]

    reference_uri = data_uri(args.reference)
    cases: list[dict[str, Any]] = [
        {"name": "plain_short", "text": "你好世界。"},
        {"name": "plain_one_character", "text": "好。"},
        {"name": "plain_symbols", "text": "你好，世界！"},
        {
            "name": "cross_language_reference",
            "text": "欢迎体验我们的新产品，它将为您带来更加简单和温暖的使用体验。",
            "reference_uri": reference_uri,
        },
    ]
    if args.same_language_reference and args.same_language_text:
        cases.append(
            {
                "name": "same_language_ultimate",
                "text": "这是一次短音频稳定性测试。",
                "reference_uri": data_uri(args.same_language_reference),
                "reference_text": args.same_language_text,
            }
        )

    report_cases = []
    for case in cases:
        # Warm model and speaker-conditioning caches outside measured samples.
        run_one(
            args.api_base,
            args.asr_base,
            model,
            case,
            args.seed,
            args.timeout,
        )
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    run_one,
                    args.api_base,
                    args.asr_base,
                    model,
                    case,
                    args.seed + index,
                    args.timeout,
                )
                for index in range(args.repeats)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        report_cases.append(summarize(case["name"], results))

    total = sum(case["requests"] for case in report_cases)
    exact = sum(case["content_exact_count"] for case in report_cases)
    extra = sum(case["extra_speech_count"] for case in report_cases)
    passed = bool(
        exact / max(1, total) >= 0.98
        and extra <= max(1, total // 100)
        and all(case["p99_duration_ratio"] <= 1.8 for case in report_cases)
        and all(case["riff_pass_count"] == case["requests"] for case in report_cases)
    )
    report = {
        "passed": passed,
        "model": model,
        "concurrency": args.concurrency,
        "requests": total,
        "content_exact_rate": round(exact / max(1, total), 4),
        "extra_speech_count": extra,
        "cases": report_cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
