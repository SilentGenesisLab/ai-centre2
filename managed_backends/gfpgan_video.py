from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import cv2
import torchvision.transforms.functional as torchvision_functional

from temp_media import allocate_work_directory, cleanup_success, mark_failed


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
    temporary = allocate_work_directory("gfpgan-frames")
    silent_video = temporary / f"{uuid4().hex}-restored-silent.mp4"
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = cv2.VideoWriter(
        str(silent_video),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if width <= 0 or height <= 0 or not writer.isOpened():
        capture.release()
        cleanup_success(temporary)
        raise RuntimeError("unable to create GFPGAN intermediate video")
    failure: BaseException | None = None
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
            if restored is None:
                raise RuntimeError(f"unable to restore frame {frame_count}")
            writer.write(restored)
        if frame_count == 0:
            raise RuntimeError("MuseTalk result video has no frames")
        writer.release()

        command = [
            str(ffmpeg),
            "-y",
            "-v",
            "warning",
            "-i",
            str(silent_video),
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
    except BaseException as exc:
        failure = exc
        raise
    finally:
        capture.release()
        writer.release()
        if failure is None:
            cleanup_success(temporary)
        else:
            mark_failed(temporary, failure)


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
