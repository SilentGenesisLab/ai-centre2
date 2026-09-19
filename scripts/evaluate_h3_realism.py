from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def evaluate(path: Path) -> dict:
    capture = cv2.VideoCapture(str(path))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    sample_every = max(1, round(fps / 2))
    face_areas: list[float] = []
    highlights: list[float] = []
    textures: list[float] = []
    skin_saturation: list[float] = []
    exposure_clipping: list[float] = []
    face_frames: list[np.ndarray] = []
    sampled = 0
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        index += 1
        if index % sample_every:
            continue
        sampled += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        exposure_clipping.append(float(np.mean(gray >= 250)))
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(max(48, width // 12), max(48, height // 12)))
        if not len(faces):
            continue
        x, y, w, h = max(faces, key=lambda item: item[2] * item[3])
        face_areas.append(float((w * h) / (width * height)))
        roi = frame[y:y + h, x:x + w]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        # Low-saturation, near-white facial pixels approximate broad specular clipping.
        highlights.append(float(np.mean((hsv[:, :, 2] > 220) & (hsv[:, :, 1] < 115))))
        skin_saturation.append(float(np.mean(hsv[:, :, 1]) / 255.0))
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        textures.append(float(cv2.Laplacian(roi_gray, cv2.CV_64F).var()))
        face_frames.append(cv2.resize(roi_gray, (128, 128), interpolation=cv2.INTER_AREA))
    capture.release()

    flicker: list[float] = []
    for previous, current in zip(face_frames, face_frames[1:]):
        # Normalize exposure so this represents structural temporal instability more than lighting changes.
        a = cv2.equalizeHist(previous)
        b = cv2.equalizeHist(current)
        flicker.append(float(np.mean(cv2.absdiff(a, b)) / 255.0))
    area_mean = float(np.mean(face_areas)) if face_areas else 0.0
    return {
        "file": str(path),
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "frames": total,
        "duration_seconds": round(total / fps, 3) if fps else None,
        "sampled_frames": sampled,
        "face_detection_rate": round(len(face_areas) / sampled, 4) if sampled else 0.0,
        "face_area_mean": round(area_mean, 5),
        "face_area_cv": round(float(np.std(face_areas) / area_mean), 4) if area_mean else None,
        "specular_highlight_ratio": round(float(np.mean(highlights)), 5) if highlights else None,
        "face_texture_laplacian": round(float(np.mean(textures)), 3) if textures else None,
        "face_saturation_mean": round(float(np.mean(skin_saturation)), 4) if skin_saturation else None,
        "exposure_clip_ratio": round(float(np.mean(exposure_clipping)), 5) if exposure_clipping else None,
        "face_temporal_difference": round(float(np.mean(flicker)), 5) if flicker else None,
        "notes": "Heuristic screening metrics; compare only same-source runs and confirm with blinded review.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"items": [evaluate(path) for path in args.videos]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
