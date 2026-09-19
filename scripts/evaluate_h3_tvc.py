from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np


def probe(path: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    return json.loads(subprocess.check_output(command, text=True, encoding="utf-8"))


def evaluate(path: Path) -> dict:
    metadata = probe(path)
    video_stream = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    audio_stream = next(
        (stream for stream in metadata["streams"] if stream["codec_type"] == "audio"), None
    )
    capture = cv2.VideoCapture(str(path))
    brightness: list[float] = []
    saturation: list[float] = []
    sharpness: list[float] = []
    shadow_clip: list[float] = []
    highlight_clip: list[float] = []
    frame_differences: list[float] = []
    previous: np.ndarray | None = None
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        brightness.append(float(gray.mean() / 255.0))
        saturation.append(float(hsv[:, :, 1].mean() / 255.0))
        sharpness.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        shadow_clip.append(float(np.mean(gray <= 3)))
        highlight_clip.append(float(np.mean(gray >= 252)))
        small = cv2.resize(gray, (96, 160), interpolation=cv2.INTER_AREA).astype(np.float32)
        if previous is not None:
            frame_differences.append(float(np.mean(np.abs(small - previous)) / 255.0))
        previous = small
    capture.release()
    fps_text = video_stream.get("avg_frame_rate", "0/1")
    numerator, denominator = (float(value) for value in fps_text.split("/"))
    fps = numerator / denominator if denominator else 0.0
    duration = float(video_stream.get("duration") or metadata["format"].get("duration") or 0)
    return {
        "file": str(path),
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": round(fps, 3),
        "frames": len(brightness),
        "duration_seconds": round(duration, 3),
        "bit_rate_kbps": round(float(metadata["format"].get("bit_rate", 0)) / 1000, 1),
        "has_audio": audio_stream is not None,
        "audio_sample_rate": int(audio_stream["sample_rate"]) if audio_stream else None,
        "brightness_mean": round(float(np.mean(brightness)), 5),
        "brightness_temporal_std": round(float(np.std(brightness)), 5),
        "saturation_mean": round(float(np.mean(saturation)), 5),
        "sharpness_laplacian_median": round(float(np.median(sharpness)), 3),
        "shadow_clip_ratio": round(float(np.mean(shadow_clip)), 6),
        "highlight_clip_ratio": round(float(np.mean(highlight_clip)), 6),
        "temporal_frame_difference_mean": round(float(np.mean(frame_differences)), 5),
        "near_duplicate_transition_ratio": round(
            float(np.mean(np.asarray(frame_differences) < 0.002)), 5
        ),
        "notes": "Technical screening only; product identity, material physics, prompt adherence and commercial usability require blinded review.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("videos", nargs="+")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = {"items": [evaluate(Path(item)) for item in args.videos]}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
