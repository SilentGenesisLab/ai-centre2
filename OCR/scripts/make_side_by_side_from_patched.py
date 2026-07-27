from __future__ import annotations

import argparse
import time
from pathlib import Path

from PIL import Image, ImageDraw

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = None


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create left-original/right-patched comparison images.")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--patched-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    patched_dir = args.patched_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source_images = [
        path
        for path in sorted(source_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    iterator = tqdm(source_images, desc="side-by-side") if tqdm else source_images
    written = 0
    started = time.perf_counter()
    for source_path in iterator:
        rel = source_path.relative_to(source_dir)
        patched_path = patched_dir / rel
        if not patched_path.is_file():
            continue
        out_path = output_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_comparison(source_path, patched_path, out_path)
        written += 1
    print({
        "source_dir": str(source_dir),
        "patched_dir": str(patched_dir),
        "output_dir": str(output_dir),
        "written": written,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    })


def _write_comparison(source_path: Path, patched_path: Path, out_path: Path) -> None:
    with Image.open(source_path) as source_image:
        left = source_image.convert("RGB")
    with Image.open(patched_path) as patched_image:
        right = patched_image.convert("RGB")
    height = max(left.height, right.height)
    canvas = Image.new("RGB", (left.width + right.width, height), (0, 0, 0))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width, 0))
    draw = ImageDraw.Draw(canvas)
    draw.line([(left.width, 0), (left.width, height)], fill=(255, 0, 0), width=2)
    canvas.save(out_path, quality=95)


if __name__ == "__main__":
    main()
