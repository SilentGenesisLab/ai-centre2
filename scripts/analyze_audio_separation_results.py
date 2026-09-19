#!/usr/bin/env python3
"""Aggregate Bandit v2 multilingual validation artifacts into JSON and CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import unicodedata
from pathlib import Path


LANGUAGE_NAMES = {
    "zh": "中文",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "th": "泰语",
    "en": "英语",
    "ru": "俄语",
    "nn": "无可识别语音",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transcript(payload: dict) -> str:
    return " ".join(str(segment.get("text", "")).strip() for segment in payload.get("segments", [])).strip()


def words(payload: dict) -> list[dict]:
    return [word for segment in payload.get("segments", []) for word in segment.get("words", [])]


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in value if character.isalnum())


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def parse_resource(path: Path) -> dict:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip()
    return {
        "max_rss_kib": int(values.get("Maximum resident set size (kbytes)", "0")),
        "cpu_percent": float(values.get("Percent of CPU this job got", "0%").rstrip("%") or 0),
        "wall_clock": values.get("Elapsed (wall clock) time (h:mm:ss or m:ss)", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report_root", type=Path)
    args = parser.parse_args()
    root = args.report_root
    fixtures = load_json(root / "fixtures.json")
    fixtures.append(
        {
            "fixture_id": "fixture-014",
            "source_group": "补充葡萄牙语（可控合成）",
            "source_file": "AI Centre VoxCPM2 Portuguese speech + fixture-013 music",
            "source_name": "葡萄牙语可控混音",
            "duration_seconds": 14.24,
            "sample_rate": 48000,
            "channels": 2,
            "expected_language": "pt",
            "controlled": True,
        }
    )

    rows: list[dict] = []
    resource_rows: list[dict] = []
    for fixture in fixtures:
        fixture_id = fixture["fixture_id"]
        metrics = load_json(root / "outputs" / fixture_id / "metrics.json")
        asr = {
            kind: load_json(root / "asr" / f"{fixture_id}-{kind}.json")
            for kind in ("mix", "speech", "background")
        }
        asr_words = {kind: words(payload) for kind, payload in asr.items()}
        average_confidence = {
            kind: statistics.fmean(float(word.get("probability", 0)) for word in word_list)
            if word_list
            else None
            for kind, word_list in asr_words.items()
        }
        mix_text = normalize_text(transcript(asr["mix"]))
        speech_text = normalize_text(transcript(asr["speech"]))
        transcript_delta = edit_distance(mix_text, speech_text) / max(len(mix_text), 1)
        row = {
            **fixture,
            "source_sha256": sha256_file(Path(fixture["source_file"])),
            "prepared_audio_sha256": sha256_file(Path(fixture["audio_file"]))
            if fixture.get("audio_file")
            else None,
            "detected_language_mix": asr["mix"].get("language"),
            "detected_language_speech": asr["speech"].get("language"),
            "language_name": LANGUAGE_NAMES.get(asr["mix"].get("language"), asr["mix"].get("language")),
            "language_probability_mix": asr["mix"].get("language_probability"),
            "language_probability_speech": asr["speech"].get("language_probability"),
            "mix_word_count": len(asr_words["mix"]),
            "speech_word_count": len(asr_words["speech"]),
            "background_word_count": len(asr_words["background"]),
            "mix_word_confidence": average_confidence["mix"],
            "speech_word_confidence": average_confidence["speech"],
            "background_word_confidence": average_confidence["background"],
            "word_retention_ratio": len(asr_words["speech"]) / max(len(asr_words["mix"]), 1),
            "background_leak_word_ratio": len(asr_words["background"]) / max(len(asr_words["mix"]), 1),
            "mix_speech_transcript_delta": transcript_delta,
            "inference_seconds": metrics["inference_seconds"],
            "rtf": metrics["rtf"],
            "reconstruction_snr_db": metrics["reconstruction_snr_db"],
            "peak_cuda_allocated_mib": metrics["peak_cuda_allocated_mib"],
            "peak_cuda_reserved_mib": metrics["peak_cuda_reserved_mib"],
            "mix_rms_dbfs": metrics["mix"]["rms_dbfs"],
            "speech_rms_dbfs": metrics["speech"]["rms_dbfs"],
            "background_rms_dbfs": metrics["background"]["rms_dbfs"],
            "speech_peak_dbfs": metrics["speech"]["peak_dbfs"],
            "speech_clipped_fraction": metrics["speech"]["clipped_fraction"],
            "mix_transcript": transcript(asr["mix"]),
            "speech_transcript": transcript(asr["speech"]),
            "background_transcript": transcript(asr["background"]),
        }
        if fixture_id == "fixture-014":
            row["controlled_metrics"] = load_json(
                root / "outputs" / fixture_id / "controlled_metrics.json"
            )
            portuguese_reference = normalize_text(
                "Olá, este é um teste controlado de separação de voz em português. "
                "A tecnologia deve preservar cada palavra com clareza, mesmo quando existe música ao fundo. "
                "Também vamos verificar se a voz não aparece na faixa de acompanhamento."
            )
            row["controlled_mix_cer"] = edit_distance(
                portuguese_reference, mix_text
            ) / len(portuguese_reference)
            row["controlled_speech_cer"] = edit_distance(
                portuguese_reference, speech_text
            ) / len(portuguese_reference)
        rows.append(row)
        resource_rows.append({"fixture_id": fixture_id, **parse_resource(root / "outputs" / fixture_id / "resource.txt")})

    real_rows = [row for row in rows if not row.get("controlled")]
    spoken_real_rows = [row for row in real_rows if row["mix_word_count"] > 0 and row["detected_language_mix"] != "ru"]
    summary = {
        "generated_at": "2026-08-20T16:55:00+08:00",
        "model": "Bandit v2 DnR v3 multilingual (checkpoint-multi.slim.pt)",
        "model_sha256": "ba9ba16504cd5d987a8c01a00307afba1340f251c18c70406c787bf76e2c4102",
        "real_video_count": len(real_rows),
        "controlled_fixture_count": len(rows) - len(real_rows),
        "total_duration_seconds": sum(float(row["duration_seconds"]) for row in rows),
        "total_inference_seconds": sum(float(row["inference_seconds"]) for row in rows),
        "inference_p50_seconds": percentile([float(row["inference_seconds"]) for row in rows], 0.5),
        "inference_p95_seconds": percentile([float(row["inference_seconds"]) for row in rows], 0.95),
        "rtf_p50": percentile([float(row["rtf"]) for row in rows], 0.5),
        "rtf_p95": percentile([float(row["rtf"]) for row in rows], 0.95),
        "reconstruction_snr_min_db": min(float(row["reconstruction_snr_db"]) for row in rows),
        "reconstruction_snr_mean_db": statistics.fmean(float(row["reconstruction_snr_db"]) for row in rows),
        "peak_cuda_allocated_mib": max(float(row["peak_cuda_allocated_mib"]) for row in rows),
        "peak_cuda_reserved_mib": max(float(row["peak_cuda_reserved_mib"]) for row in rows),
        "spoken_word_retention_ratio": sum(row["speech_word_count"] for row in spoken_real_rows)
        / max(sum(row["mix_word_count"] for row in spoken_real_rows), 1),
        "spoken_background_leak_word_ratio": sum(row["background_word_count"] for row in spoken_real_rows)
        / max(sum(row["mix_word_count"] for row in spoken_real_rows), 1),
        "rows": rows,
    }
    (root / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "resources.json").write_text(json.dumps(resource_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_fields = [key for key in rows[0] if key not in {"controlled_metrics", "mix_transcript", "speech_transcript", "background_transcript"}]
    with (root / "results.csv").open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({key: value for key, value in summary.items() if key != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
