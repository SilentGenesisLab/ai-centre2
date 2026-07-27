from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ocr_service.config import load_config
from ocr_service.engine import PaddleOcrEngine
from ocr_service.schemas import SubtitleDetectConfig
from ocr_service.subtitle_detector import detect_subtitle_events


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect and white-mask precise title/subtitle events.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-video", type=Path)
    source.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-lang-hint", default="es")
    parser.add_argument("--mode", choices=["fast", "balanced", "accurate"], default="balanced")
    parser.add_argument("--no-debug-video", action="store_true")
    parser.add_argument("--coarse-interval-seconds", type=float)
    parser.add_argument("--boundary-window-seconds", type=float, default=0.3)
    parser.add_argument("--min-ocr-score", type=float, default=0.45)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.input_video:
        videos = [args.input_video.resolve()]
        source_root = args.input_video.resolve().parent
    else:
        source_root = args.input_dir.resolve()
        videos = [
            path for path in sorted(source_root.rglob("*"))
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        ]
    if not videos:
        raise SystemExit("No video files found.")

    settings = SubtitleDetectConfig(
        coarse_interval_seconds=args.coarse_interval_seconds,
        boundary_window_seconds=args.boundary_window_seconds,
        min_ocr_score=args.min_ocr_score,
    )
    engine = PaddleOcrEngine(load_config())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    batch_started = time.perf_counter()
    for index, video in enumerate(videos, start=1):
        relative = video.relative_to(source_root)
        case_name = f"{index:03d}_{relative.stem}"
        case_output = args.output_dir / case_name
        print(f"[{index}/{len(videos)}] {video.name}", flush=True)
        try:
            result = detect_subtitle_events(
                engine=engine,
                input_path=video,
                output_dir=case_output,
                source_lang_hint=args.source_lang_hint,
                mode=args.mode,
                export_debug_video=not args.no_debug_video,
                config=settings,
                asr_segments=[],
            )
            row = {
                "case": case_name,
                "video": str(video),
                "status": result["status"],
                **result["metrics"],
                "output_dir": result["output_dir"],
                "review_html": result["review_html"] or "",
            }
        except Exception as exc:
            row = {
                "case": case_name,
                "video": str(video),
                "status": "failed",
                "shots": 0,
                "tracks": 0,
                "events": 0,
                "needs_review": 0,
                "elapsed_seconds": 0,
                "output_dir": str(case_output),
                "review_html": "",
                "error": str(exc),
            }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=2), flush=True)

    summary_path = args.output_dir / "benchmark_summary.csv"
    fieldnames = sorted({key for row in rows for key in row})
    with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    index_path = write_index(args.output_dir, rows)
    print(
        json.dumps(
            {
                "videos": len(videos),
                "elapsed_seconds": round(time.perf_counter() - batch_started, 3),
                "summary": str(summary_path),
                "index": str(index_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def write_index(output_dir: Path, rows: list[dict[str, object]]) -> Path:
    cards = []
    for row in rows:
        review = row.get("review_html")
        review_link = Path(str(review)).relative_to(output_dir).as_posix() if review else ""
        cards.append(
            f"<tr><td>{row['case']}</td><td>{row['status']}</td><td>{row.get('events', 0)}</td>"
            f"<td>{row.get('elapsed_seconds', 0)}</td><td><a href=\"{review_link}\">复核</a></td></tr>"
        )
    index = output_dir / "index.html"
    index.write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Precise subtitle benchmark</title>"
        "<style>body{font-family:system-ui;background:#0b0e14;color:#edf2fa;padding:24px}table{border-collapse:collapse;width:100%}"
        "td,th{padding:10px;border-bottom:1px solid #303849;text-align:left}a{color:#79b8ff}</style></head><body>"
        "<h1>精准标题/字幕擦除基准</h1><table><thead><tr><th>Case</th><th>Status</th><th>Events</th><th>Seconds</th><th>Review</th>"
        "</tr></thead><tbody>" + "".join(cards) + "</tbody></table></body></html>",
        encoding="utf-8",
    )
    return index


if __name__ == "__main__":
    main()
