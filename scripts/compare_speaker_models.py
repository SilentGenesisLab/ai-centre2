#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import torch
from funasr import AutoModel


def embedding(model: AutoModel, path: Path) -> torch.Tensor:
    result = model.generate(input=str(path), disable_pbar=True)
    if not result or "spk_embedding" not in result[0]:
        raise RuntimeError(f"no embedding for {path}")
    return result[0]["spk_embedding"].detach().cpu().flatten()


def similarity(left: torch.Tensor, right: torch.Tensor) -> float:
    return float(
        torch.nn.functional.cosine_similarity(
            left.unsqueeze(0), right.unsqueeze(0)
        ).item()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-root", type=Path, required=True)
    parser.add_argument("--known-clone-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--models",
        nargs="+",
        default=[
            "iic/speech_campplus_sv_zh-cn_16k-common",
            "iic/speech_eres2netv2_sv_zh-cn_16k-common",
            "iic/speech_campplus_sv_en_voxceleb_16k",
        ],
    )
    args = parser.parse_args()
    audio = args.validation_root / "audio-samples"
    positive_pairs = [
        (audio / "reference-es-0.wav", audio / "matrix-cross-es-to-zh-medium-0.wav"),
        (audio / "reference-zh-0.wav", audio / "matrix-cross-zh-to-en-medium-0.wav"),
        (audio / "reference-ja-0.wav", audio / "matrix-cross-ja-to-zh-medium-0.wav"),
        (args.known_clone_root / "reference-only.wav", args.known_clone_root / "user-clone.wav"),
    ]
    negative_pairs = [
        (audio / "reference-es-0.wav", args.known_clone_root / "user-clone.wav"),
        (audio / "reference-zh-0.wav", args.known_clone_root / "user-clone.wav"),
        (args.known_clone_root / "reference-only.wav", audio / "matrix-cross-es-to-zh-medium-0.wav"),
        (args.known_clone_root / "reference-only.wav", audio / "matrix-cross-zh-to-en-medium-0.wav"),
    ]
    missing = [
        str(path)
        for pair in positive_pairs + negative_pairs
        for path in pair
        if not path.is_file()
    ]
    if missing:
        raise SystemExit(f"missing inputs: {missing}")

    report: dict[str, object] = {"models": []}
    for model_name in args.models:
        print(f"loading {model_name}", flush=True)
        try:
            model = AutoModel(model=model_name, device="cpu", disable_update=True)
        except Exception as exc:
            item = {"model": model_name, "error": type(exc).__name__}
            report["models"].append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            continue
        paths = {path for pair in positive_pairs + negative_pairs for path in pair}
        embeddings = {path: embedding(model, path) for path in paths}
        positives = [similarity(embeddings[left], embeddings[right]) for left, right in positive_pairs]
        negatives = [similarity(embeddings[left], embeddings[right]) for left, right in negative_pairs]
        item = {
            "model": model_name,
            "positive_scores": positives,
            "negative_scores": negatives,
            "positive_mean": statistics.fmean(positives),
            "positive_min": min(positives),
            "negative_mean": statistics.fmean(negatives),
            "negative_max": max(negatives),
            "separation": min(positives) - max(negatives),
        }
        report["models"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
