from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = None


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cover OCR-detected text in videos and export side-by-side comparisons.")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8096/v1/ocr/batch")
    parser.add_argument("--ocr-fps", type=float, default=4.0, help="OCR sample rate. Boxes are reused between samples.")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--expand", type=int, default=4)
    parser.add_argument("--merge-lines", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--line-gap", type=int, default=18, help="Merge OCR boxes whose vertical centers are close.")
    parser.add_argument("--line-x-expand", type=int, default=18)
    parser.add_argument("--line-y-expand", type=int, default=10)
    parser.add_argument("--temporal-mode", choices=["nearest", "previous"], default="nearest")
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--max-seconds", type=float, default=0.0, help="Optional duration cap per video; 0 means full video.")
    parser.add_argument("--crf", type=int, default=23)
    parser.add_argument("--preset", default="veryfast")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    videos = [path for path in sorted(input_dir.rglob("*")) if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES]
    if not videos:
        raise SystemExit(f"No videos found in {input_dir}")

    started = time.perf_counter()
    rows = []
    for video_path in videos:
        row = process_video(video_path, input_dir, output_dir, args)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=2))

    summary_csv = output_dir / "video_patch_summary.csv"
    with summary_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "video",
                "status",
                "duration",
                "fps",
                "ocr_samples",
                "total_boxes",
                "elapsed_seconds",
                "output_video",
                "events_json",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(json.dumps({
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "video_count": len(videos),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "summary_csv": str(summary_csv),
    }, ensure_ascii=False, indent=2))


def process_video(video_path: Path, input_dir: Path, output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    rel = video_path.relative_to(input_dir)
    stem = rel.with_suffix("").as_posix().replace("/", "__")
    out_video = output_dir / f"{stem}_side_by_side_patch.mp4"
    events_json = output_dir / f"{stem}_ocr_events.json"
    tmp_no_audio = output_dir / f".{stem}_no_audio.mp4"
    started = time.perf_counter()

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return _row(video_path, "failed_open", 0, 0, 0, 0, 0, out_video, events_json)

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
    if args.max_seconds > 0:
        duration = min(duration, args.max_seconds)
        frame_count = int(round(duration * fps))

    sample_step = max(1, int(round(fps / max(args.ocr_fps, 0.1))))
    sample_frames = list(range(0, frame_count, sample_step))
    detections = collect_detections(
        capture=capture,
        video_path=video_path,
        sample_frames=sample_frames,
        fps=fps,
        api_url=args.api_url,
        batch_size=args.batch_size,
        expand=args.expand,
        min_score=args.min_score,
        width=width,
        height=height,
        merge_lines=args.merge_lines,
        line_gap=args.line_gap,
        line_x_expand=args.line_x_expand,
        line_y_expand=args.line_y_expand,
    )

    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_no_audio), fourcc, fps, (width * 2, height))
    if not writer.isOpened():
        capture.release()
        return _row(video_path, "failed_writer", duration, fps, len(sample_frames), 0, started, out_video, events_json)

    progress = range(frame_count)
    if tqdm is not None:
        progress = tqdm(progress, desc=video_path.name[:32], unit="frame")

    detection_frames = [item["frame_index"] for item in detections]
    nearest_detection_index = 0
    current_boxes: list[dict[str, Any]] = []
    total_written_boxes = 0
    detection_by_frame = {item["frame_index"]: item for item in detections}
    for frame_index in progress:
        ok, frame = capture.read()
        if not ok:
            break
        if args.temporal_mode == "nearest" and detection_frames:
            while nearest_detection_index + 1 < len(detection_frames):
                current_distance = abs(frame_index - detection_frames[nearest_detection_index])
                next_distance = abs(frame_index - detection_frames[nearest_detection_index + 1])
                if next_distance >= current_distance:
                    break
                nearest_detection_index += 1
            current_boxes = detections[nearest_detection_index]["items"]
        elif frame_index in detection_by_frame:
            current_boxes = detection_by_frame[frame_index]["items"]
        patched = frame.copy()
        for item in current_boxes:
            x1, y1, x2, y2 = item["bbox"]
            cv2.rectangle(patched, (x1, y1), (x2, y2), (255, 255, 255), thickness=-1)
        total_written_boxes += len(current_boxes)
        writer.write(np.hstack([frame, patched]))

    capture.release()
    writer.release()
    events_json.write_text(json.dumps({
        "source_video": str(video_path),
        "fps": fps,
        "duration": duration,
        "ocr_fps": args.ocr_fps,
        "expand": args.expand,
        "merge_lines": args.merge_lines,
        "temporal_mode": args.temporal_mode,
        "detections": detections,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    mux_audio(video_path, tmp_no_audio, out_video, args.crf, args.preset)
    tmp_no_audio.unlink(missing_ok=True)
    total_boxes = sum(len(item["items"]) for item in detections)
    return _row(video_path, "succeeded", duration, fps, len(sample_frames), total_boxes, started, out_video, events_json)


def collect_detections(
    capture: cv2.VideoCapture,
    video_path: Path,
    sample_frames: list[int],
    fps: float,
    api_url: str,
    batch_size: int,
    expand: int,
    min_score: float,
    width: int,
    height: int,
    merge_lines: bool,
    line_gap: int,
    line_x_expand: int,
    line_y_expand: int,
) -> list[dict[str, Any]]:
    detections: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ocr_video_frames_") as temp_name:
        temp_dir = Path(temp_name)
        for batch_start in range(0, len(sample_frames), batch_size):
            frame_indices = sample_frames[batch_start : batch_start + batch_size]
            image_payload = []
            frame_paths: dict[int, Path] = {}
            for frame_index in frame_indices:
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = capture.read()
                if not ok:
                    continue
                frame_path = temp_dir / f"frame_{frame_index:06d}.jpg"
                cv2.imwrite(str(frame_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                frame_paths[frame_index] = frame_path
                image_payload.append({
                    "image_id": str(frame_index),
                    "path": frame_path.as_posix(),
                    "time": round(frame_index / fps, 3),
                    "regions": [{"name": "full", "bbox": None}],
                })
            if not image_payload:
                continue
            response = requests.post(api_url, json={"job_id": f"video-{video_path.stem}-{batch_start}", "images": image_payload}, timeout=300)
            response.raise_for_status()
            by_image = {item["image_id"]: item for item in response.json().get("results", [])}
            for frame_index in frame_indices:
                raw_items = by_image.get(str(frame_index), {}).get("items", [])
                items = []
                for raw in raw_items:
                    score = float(raw.get("score", 0.0))
                    if score < min_score:
                        continue
                    bbox = [int(round(value)) for value in raw.get("bbox", [0, 0, 0, 0])]
                    x1, y1, x2, y2 = expand_box(bbox, expand, width, height)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    items.append({
                        "bbox": [x1, y1, x2, y2],
                        "raw_bbox": bbox,
                        "text": raw.get("text", ""),
                        "score": score,
                        "region": raw.get("region", "full"),
                    })
                if merge_lines:
                    items = merge_text_lines(items, width, height, line_gap, line_x_expand, line_y_expand)
                detections.append({"frame_index": frame_index, "time": round(frame_index / fps, 3), "items": items})
    return detections


def merge_text_lines(
    items: list[dict[str, Any]],
    width: int,
    height: int,
    line_gap: int,
    x_expand: int,
    y_expand: int,
) -> list[dict[str, Any]]:
    if not items:
        return []
    sorted_items = sorted(items, key=lambda item: ((item["bbox"][1] + item["bbox"][3]) / 2, item["bbox"][0]))
    lines: list[list[dict[str, Any]]] = []
    for item in sorted_items:
        center_y = (item["bbox"][1] + item["bbox"][3]) / 2
        if lines:
            last_line = lines[-1]
            last_center_y = sum((line_item["bbox"][1] + line_item["bbox"][3]) / 2 for line_item in last_line) / len(last_line)
            if abs(center_y - last_center_y) <= line_gap:
                last_line.append(item)
                continue
        lines.append([item])

    merged: list[dict[str, Any]] = []
    for line in lines:
        x1 = min(item["bbox"][0] for item in line)
        y1 = min(item["bbox"][1] for item in line)
        x2 = max(item["bbox"][2] for item in line)
        y2 = max(item["bbox"][3] for item in line)
        x1, y1, x2, y2 = expand_box([x1, y1, x2, y2], 0, width, height)
        x1 = max(0, x1 - x_expand)
        x2 = min(width - 1, x2 + x_expand)
        y1 = max(0, y1 - y_expand)
        y2 = min(height - 1, y2 + y_expand)
        merged.append({
            "bbox": [x1, y1, x2, y2],
            "raw_bbox": [min(item["raw_bbox"][0] for item in line), min(item["raw_bbox"][1] for item in line), max(item["raw_bbox"][2] for item in line), max(item["raw_bbox"][3] for item in line)],
            "text": " ".join(item.get("text", "") for item in line).strip(),
            "score": min(float(item.get("score", 0.0)) for item in line),
            "region": "merged_line",
            "source_count": len(line),
        })
    return merged


def expand_box(bbox: list[int], expand: int, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return (
        max(0, x1 - expand),
        max(0, y1 - expand),
        min(width - 1, x2 + expand),
        min(height - 1, y2 + expand),
    )


def mux_audio(source_video: Path, silent_video: Path, output_video: Path, crf: int, preset: str) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        silent_video.replace(output_video)
        return
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(silent_video),
        "-i",
        str(source_video),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-shortest",
        str(output_video),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _row(
    video_path: Path,
    status: str,
    duration: float,
    fps: float,
    ocr_samples: int,
    total_boxes: int,
    started: float | int,
    output_video: Path,
    events_json: Path,
) -> dict[str, Any]:
    elapsed = 0.0 if not started else time.perf_counter() - float(started)
    return {
        "video": str(video_path),
        "status": status,
        "duration": round(duration, 3),
        "fps": round(fps, 3),
        "ocr_samples": ocr_samples,
        "total_boxes": total_boxes,
        "elapsed_seconds": round(elapsed, 3),
        "output_video": str(output_video),
        "events_json": str(events_json),
    }


if __name__ == "__main__":
    main()
