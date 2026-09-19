#!/usr/bin/env python3
"""Build a Bandit-vs-MDX representative-sample comparison report."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf


FIXTURE_IDS = ("fixture-003", "fixture-006", "fixture-013", "fixture-014", "fixture-015")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def words(payload: dict) -> list[dict]:
    return [word for segment in payload.get("segments", []) for word in segment.get("words", [])]


def transcript(payload: dict) -> str:
    return " ".join(str(segment.get("text", "")).strip() for segment in payload.get("segments", [])).strip()


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in value if character.isalnum())


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def parse_wall_clock(value: str) -> float:
    fields = [float(field) for field in value.split(":")]
    if len(fields) == 2:
        return fields[0] * 60 + fields[1]
    if len(fields) == 3:
        return fields[0] * 3600 + fields[1] * 60 + fields[2]
    raise ValueError(f"unsupported wall-clock value: {value}")


def resource_metrics(path: Path, fallback: Path) -> dict:
    source = path if path.exists() else fallback
    content = source.read_text(encoding="utf-8", errors="replace")

    def last(pattern: str, default: str) -> str:
        matches = re.findall(pattern, content)
        return matches[-1].strip() if matches else default

    wall_clock = last(r"Elapsed \(wall clock\) time \(h:mm:ss or m:ss\): (.+)", "0:00")
    return {
        "wall_seconds": parse_wall_clock(wall_clock),
        "cpu_percent": float(last(r"Percent of CPU this job got: (.+)%", "0")),
        "max_rss_mib": int(last(r"Maximum resident set size \(kbytes\): (\d+)", "0")) / 1024,
    }


def si_sdr(reference: np.ndarray, estimate: np.ndarray) -> float:
    reference = reference.astype(np.float64).reshape(-1)
    estimate = estimate.astype(np.float64).reshape(-1)
    length = min(len(reference), len(estimate))
    reference = reference[:length] - np.mean(reference[:length])
    estimate = estimate[:length] - np.mean(estimate[:length])
    projection = np.dot(estimate, reference) * reference / (np.dot(reference, reference) + 1e-20)
    noise = estimate - projection
    return float(10 * np.log10((np.dot(projection, projection) + 1e-20) / (np.dot(noise, noise) + 1e-20)))


def read_audio(path: Path) -> np.ndarray:
    audio, _ = sf.read(path, dtype="float32", always_2d=True)
    return audio


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report_root", type=Path)
    args = parser.parse_args()
    root = args.report_root
    rows = []

    for fixture_id in FIXTURE_IDS:
        mdx_root = root / "mdx-ab" / fixture_id
        mdx_metrics = load_json(mdx_root / "metrics.json")
        resources = resource_metrics(mdx_root / "resource.txt", mdx_root / "run.log")
        bandit_metrics = load_json(root / "outputs" / fixture_id / "metrics.json")
        bandit_resources = resource_metrics(
            root / "outputs" / fixture_id / "resource.txt",
            root / "outputs" / fixture_id / "resource.txt",
        )
        mix_asr = load_json(root / "asr" / f"{fixture_id}-mix.json")
        bandit_speech_asr = load_json(root / "asr" / f"{fixture_id}-speech.json")
        bandit_background_asr = load_json(root / "asr" / f"{fixture_id}-background.json")
        mdx_vocals_asr = load_json(mdx_root / "asr-vocals.json")
        mdx_instrumental_asr = load_json(mdx_root / "asr-instrumental.json")
        mix_word_count = len(words(mix_asr))
        mix_text = normalize(transcript(mix_asr))

        def comparison(speech_payload: dict, background_payload: dict) -> dict:
            speech_word_count = len(words(speech_payload))
            background_word_count = len(words(background_payload))
            speech_text = normalize(transcript(speech_payload))
            return {
                "speech_language": speech_payload.get("language"),
                "speech_word_count": speech_word_count,
                "background_word_count": background_word_count,
                "word_retention_ratio": speech_word_count / max(mix_word_count, 1),
                "background_leak_word_ratio": background_word_count / max(mix_word_count, 1),
                "transcript_delta": edit_distance(mix_text, speech_text) / max(len(mix_text), 1),
                "speech_transcript": transcript(speech_payload),
                "background_transcript": transcript(background_payload),
            }

        row = {
            "fixture_id": fixture_id,
            "duration_seconds": mdx_metrics["duration_seconds"],
            "mix_language": mix_asr.get("language"),
            "mix_word_count": mix_word_count,
            "bandit": {
                **comparison(bandit_speech_asr, bandit_background_asr),
                **bandit_resources,
                "rtf_wall": bandit_resources["wall_seconds"] / mdx_metrics["duration_seconds"],
                "rtf_inference": bandit_metrics["rtf"],
                "reconstruction_snr_db": bandit_metrics["reconstruction_snr_db"],
                "backend": "pytorch-cuda",
            },
            "mdx": {
                **comparison(mdx_vocals_asr, mdx_instrumental_asr),
                **resources,
                "rtf_wall": resources["wall_seconds"] / mdx_metrics["duration_seconds"],
                "reconstruction_snr_db": mdx_metrics["reconstruction_snr_db"],
                "backend": mdx_metrics["backend"],
            },
        }

        if fixture_id == "fixture-014" and (root / "inputs" / "pt-speech-reference.wav").exists():
            clean_speech = read_audio(root / "inputs" / "pt-speech-reference.wav")
            clean_background = read_audio(root / "inputs" / "pt-background-reference.wav")
            mix = read_audio(root / "inputs" / "source-audio-v2" / "fixture-014.wav")
            vocals_path = next(mdx_root.glob("*(Vocals)*.wav"))
            instrumental_path = next(mdx_root.glob("*(Instrumental)*.wav"))
            vocals = read_audio(vocals_path)
            instrumental = read_audio(instrumental_path)
            row["mdx"]["controlled_metrics"] = {
                "speech_mix_si_sdr_db": si_sdr(clean_speech, mix),
                "speech_est_si_sdr_db": si_sdr(clean_speech, vocals),
                "speech_si_sdri_db": si_sdr(clean_speech, vocals) - si_sdr(clean_speech, mix),
                "background_mix_si_sdr_db": si_sdr(clean_background, mix),
                "background_est_si_sdr_db": si_sdr(clean_background, instrumental),
                "background_si_sdri_db": si_sdr(clean_background, instrumental) - si_sdr(clean_background, mix),
            }
        rows.append(row)

    summary = {
        "generated_at": "2026-08-20T16:50:00+08:00",
        "bandit_model": "Bandit v2 DnR v3 multilingual",
        "mdx_model": "UVR-MDX-NET Inst HQ 5",
        "mdx_model_sha256": "811cb24095d865763752310848b7ec86aeede0626cb05749ab35350e46897000",
        "mdx_backend": "onnxruntime-cpu",
        "sample_count": len(rows),
        "mdx_total_wall_seconds": sum(row["mdx"]["wall_seconds"] for row in rows),
        "mdx_rtf_wall_mean": statistics.fmean(row["mdx"]["rtf_wall"] for row in rows),
        "mdx_max_rss_mib": max(row["mdx"]["max_rss_mib"] for row in rows),
        "rows": rows,
    }
    (root / "mdx-ab-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
