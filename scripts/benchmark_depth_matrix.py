from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from decord import VideoReader, cpu

from control_plane.config import get_settings
from control_plane.depth_inference import (
    DepthAnything3Inference,
    VideoDepthAnythingInference,
)
from control_plane.depth_tasks import effective_input_size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the four video-depth model variants.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--input-size", type=int, default=518)
    parser.add_argument("--max-resolution", type=int, default=960)
    parser.add_argument("--target-fps", type=float, default=-1)
    parser.add_argument(
        "--variants",
        nargs="+",
        default=["da2:small", "da2:base", "da3:small", "da3:base"],
    )
    return parser.parse_args()


def build_model(version: str, model_size: str):
    settings = get_settings()
    if version == "da2":
        checkpoint = (
            settings.depth_checkpoint_path
            if model_size == "small"
            else settings.depth_da2_base_checkpoint_path
        )
        return VideoDepthAnythingInference(
            settings.depth_source_dir,
            checkpoint,
            model_size,
        )
    if version == "da3":
        model_dir = (
            settings.depth_da3_small_model_dir
            if model_size == "small"
            else settings.depth_da3_base_model_dir
        )
        return DepthAnything3Inference(
            settings.depth_da3_source_dir,
            model_dir,
            model_size,
            chunk_size=settings.depth_da3_chunk_size,
            overlap=settings.depth_da3_chunk_overlap,
        )
    raise ValueError(f"unknown depth version: {version}")


def output_metrics(path: Path) -> dict[str, float | int | str]:
    video = VideoReader(str(path), ctx=cpu(0))
    frame_count = len(video)
    sample_count = min(frame_count, 120)
    indices = np.linspace(0, frame_count - 1, sample_count, dtype=np.int64).tolist()
    frames = video.get_batch(indices).asnumpy().astype(np.float32) / 255.0
    grayscale = frames.mean(axis=3)
    temporal_delta = (
        float(np.abs(np.diff(grayscale, axis=0)).mean()) if sample_count > 1 else 0.0
    )
    return {
        "output_frame_count": frame_count,
        "output_fps": round(float(video.get_avg_fps()), 3),
        "decoded_output_width": int(video[0].shape[1]),
        "decoded_output_height": int(video[0].shape[0]),
        "output_mean": round(float(grayscale.mean()), 6),
        "output_std": round(float(grayscale.std()), 6),
        "output_clipped_ratio": round(
            float(((grayscale <= 1 / 255) | (grayscale >= 254 / 255)).mean()), 6
        ),
        "sampled_temporal_delta": round(temporal_delta, 6),
        "output_bytes": path.stat().st_size,
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    settings = get_settings()
    for raw in args.variants:
        version, model_size = raw.split(":", 1)
        if version not in {"da2", "da3"} or model_size not in {"small", "base"}:
            raise SystemExit(f"invalid variant: {raw}")
        target = args.output_dir / f"{version}-{model_size}.mp4"
        actual_input_size = effective_input_size(
            version,
            model_size,
            args.input_size,
            settings.depth_da2_base_max_input_size,
        )
        load_started = time.perf_counter()
        model = build_model(version, model_size)
        load_seconds = time.perf_counter() - load_started
        progress_events: list[tuple[str, int]] = []
        try:
            metrics = model.process(
                args.input,
                target,
                input_size=actual_input_size,
                max_resolution=args.max_resolution,
                target_fps=args.target_fps,
                progress=lambda stage, progress: progress_events.append((stage, progress)),
            )
        finally:
            model.close()
        row: dict[str, object] = {
            "version": version,
            "model": model_size,
            "requested_input_size": args.input_size,
            "effective_input_size": actual_input_size,
            "input_size_capped": actual_input_size != args.input_size,
            "model_load_seconds": round(load_seconds, 3),
            **metrics,
            **output_metrics(target),
            "progress_events": progress_events,
        }
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    report = {
        "input": str(args.input),
        "input_size": args.input_size,
        "max_resolution": args.max_resolution,
        "target_fps": args.target_fps,
        "results": results,
    }
    (args.output_dir / "benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
