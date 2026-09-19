from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import torchvision.transforms.functional as torchvision_functional


# BasicSR 1.4 imports a torchvision module removed in torchvision 0.26.
sys.modules["torchvision.transforms.functional_tensor"] = torchvision_functional

from gfpgan import GFPGANer  # noqa: E402


def restore_video(
    input_path: Path,
    output_path: Path,
    model_path: Path,
    ffmpeg: Path,
    weight: float,
) -> None:
    capture = cv2.VideoCapture(str(input_path))
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not capture.isOpened() or fps <= 0:
        raise RuntimeError("unable to open MuseTalk result video")

    restorer = GFPGANer(
        model_path=str(model_path),
        upscale=1,
        arch="clean",
        channel_multiplier=2,
        bg_upsampler=None,
    )
    temporary = Path(tempfile.mkdtemp(prefix=".gfpgan-", dir=output_path.parent))
    try:
        frame_count = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            _, _, restored = restorer.enhance(
                frame,
                has_aligned=False,
                only_center_face=False,
                paste_back=True,
                weight=weight,
            )
            frame_count += 1
            destination = temporary / f"{frame_count:08d}.png"
            if restored is None or not cv2.imwrite(str(destination), restored):
                raise RuntimeError(f"unable to write restored frame {frame_count}")
        if frame_count == 0:
            raise RuntimeError("MuseTalk result video has no frames")

        command = [
            str(ffmpeg),
            "-y",
            "-v",
            "warning",
            "-framerate",
            f"{fps:.6f}",
            "-i",
            str(temporary / "%08d.png"),
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-c:a",
            "copy",
            "-shortest",
            str(output_path),
        ]
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0 or not output_path.is_file():
            raise RuntimeError(f"FFmpeg exited with code {completed.returncode}")
    finally:
        capture.release()
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--weight", type=float, default=0.5)
    args = parser.parse_args()
    restore_video(
        args.input,
        args.output,
        args.model_path,
        args.ffmpeg,
        args.weight,
    )


if __name__ == "__main__":
    main()
