from __future__ import annotations

import argparse
import csv
import html
import json
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = None


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cover all OCR-detected text boxes with white patches.")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8096/v1/ocr/batch")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--expand", type=int, default=4, help="Pixels to expand each OCR box before covering.")
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--job-id", default="white-patch-batch")
    parser.add_argument("--side-by-side-dir", type=Path, default=None, help="Optional output dir for left-original/right-patched comparison images.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    images = [
        path
        for path in sorted(input_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    if not images:
        raise SystemExit(f"No images found in {input_dir}")

    results_jsonl = output_dir / "ocr_results.jsonl"
    summary_csv = output_dir / "ocr_summary.csv"
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    iterator = range(0, len(images), args.batch_size)
    if tqdm is not None:
        iterator = tqdm(iterator, total=(len(images) + args.batch_size - 1) // args.batch_size, desc="OCR+patch")

    with results_jsonl.open("w", encoding="utf-8") as jsonl:
        for batch_index, start in enumerate(iterator):
            batch = images[start : start + args.batch_size]
            payload = {
                "job_id": f"{args.job_id}-{batch_index:04d}",
                "images": [
                    {
                        "image_id": str(path.relative_to(input_dir)).replace("\\", "/"),
                        "path": path.as_posix(),
                        "regions": [{"name": "full", "bbox": None}],
                    }
                    for path in batch
                ],
            }
            response = requests.post(args.api_url, json=payload, timeout=300)
            response.raise_for_status()
            data = response.json()
            by_image_id = {item["image_id"]: item for item in data.get("results", [])}
            for image_path in batch:
                rel = image_path.relative_to(input_dir)
                image_id = str(rel).replace("\\", "/")
                result = by_image_id.get(image_id, {"items": []})
                out_path = output_dir / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                kept_items = _cover_image(
                    image_path=image_path,
                    out_path=out_path,
                    items=result.get("items", []),
                    expand=args.expand,
                    min_score=args.min_score,
                )
                comparison_path = ""
                if args.side_by_side_dir is not None:
                    comparison_path = str(_write_side_by_side(
                        source_path=image_path,
                        patched_path=out_path,
                        input_dir=input_dir,
                        comparison_dir=args.side_by_side_dir.resolve(),
                    ))
                record = {
                    "image_id": image_id,
                    "source_path": str(image_path),
                    "patched_path": str(out_path),
                    "comparison_path": comparison_path,
                    "detected_count": len(result.get("items", [])),
                    "covered_count": len(kept_items),
                    "items": kept_items,
                }
                jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                rows.append(record)

    _write_summary_csv(summary_csv, rows)
    _write_review_html(output_dir / "review.html", rows)
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "image_count": len(images),
        "total_boxes": sum(row["covered_count"] for row in rows),
        "elapsed_seconds": round(elapsed, 3),
        "results_jsonl": str(results_jsonl),
        "summary_csv": str(summary_csv),
        "review_html": str(output_dir / "review.html"),
    }, ensure_ascii=False, indent=2))


def _cover_image(
    image_path: Path,
    out_path: Path,
    items: list[dict[str, Any]],
    expand: int,
    min_score: float,
) -> list[dict[str, Any]]:
    with Image.open(image_path) as img:
        image = img.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    kept: list[dict[str, Any]] = []
    for item in items:
        score = float(item.get("score", 0.0))
        if score < min_score:
            continue
        bbox = [int(round(value)) for value in item.get("bbox", [0, 0, 0, 0])]
        x1, y1, x2, y2 = _expand_box(bbox, expand, width, height)
        if x2 <= x1 or y2 <= y1:
            continue
        draw.rectangle([x1, y1, x2, y2], fill=(255, 255, 255))
        kept.append({
            "bbox": [x1, y1, x2, y2],
            "raw_bbox": bbox,
            "text": item.get("text", ""),
            "score": score,
            "region": item.get("region", "full"),
        })
    image.save(out_path, quality=95)
    return kept


def _write_side_by_side(
    source_path: Path,
    patched_path: Path,
    input_dir: Path,
    comparison_dir: Path,
) -> Path:
    rel = source_path.relative_to(input_dir)
    out_path = comparison_dir / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as source_image:
        left = source_image.convert("RGB")
    with Image.open(patched_path) as patched_image:
        right = patched_image.convert("RGB")
    height = max(left.height, right.height)
    width = left.width + right.width
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width, 0))
    draw = ImageDraw.Draw(canvas)
    draw.line([(left.width, 0), (left.width, height)], fill=(255, 0, 0), width=2)
    canvas.save(out_path, quality=95)
    return out_path


def _expand_box(bbox: list[int], expand: int, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return (
        max(0, x1 - expand),
        max(0, y1 - expand),
        min(width - 1, x2 + expand),
        min(height - 1, y2 + expand),
    )


def _write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["image_id", "covered_count", "texts", "patched_path", "comparison_path", "source_path"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "image_id": row["image_id"],
                "covered_count": row["covered_count"],
                "texts": " | ".join(item["text"] for item in row["items"]),
                "patched_path": row["patched_path"],
                "comparison_path": row.get("comparison_path", ""),
                "source_path": row["source_path"],
            })


def _write_review_html(path: Path, rows: list[dict[str, Any]]) -> None:
    cards = []
    for row in rows:
        rel_patched = Path(row["patched_path"]).relative_to(path.parent).as_posix()
        cards.append(
            f"""
            <article>
              <img src="{html.escape(rel_patched)}" loading="lazy">
              <div class="meta">{html.escape(row["image_id"])} · {row["covered_count"]} boxes</div>
              <pre>{html.escape(json.dumps(row["items"], ensure_ascii=False, indent=2))}</pre>
            </article>
            """
        )
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>OCR 白色贴片复盘</title>
  <style>
    body {{ margin: 0; background: #0b0d12; color: #f5f7fb; font-family: Arial, "Microsoft YaHei", sans-serif; }}
    header {{ position: sticky; top: 0; background: #111722; padding: 16px 22px; border-bottom: 1px solid #2b3342; }}
    main {{ padding: 18px; display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
    article {{ background: #151b26; border: 1px solid #2b3342; border-radius: 12px; padding: 12px; }}
    img {{ width: 100%; max-height: 680px; object-fit: contain; background: #000; }}
    .meta {{ color: #aab1c3; margin: 8px 0; font-size: 13px; }}
    pre {{ white-space: pre-wrap; max-height: 220px; overflow: auto; font-size: 12px; background: #090b10; padding: 8px; border-radius: 8px; }}
  </style>
</head>
<body>
<header><h1>OCR 白色贴片复盘</h1><div>{len(rows)} images · {sum(row["covered_count"] for row in rows)} boxes</div></header>
<main>
{''.join(cards)}
</main>
</body>
</html>""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
