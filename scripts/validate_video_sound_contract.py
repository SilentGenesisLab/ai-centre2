from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import httpx
import imageio_ffmpeg

from control_plane.generation_tasks import _prepare_video_for_upload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ai-centre-sound-") as temp:
        source = Path(temp) / "source.mp4"
        with httpx.stream("GET", args.url, timeout=120, follow_redirects=True) as response:
            response.raise_for_status()
            with source.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
        result = _prepare_video_for_upload(source, {"sound": False})
        probe = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(result),
                "-map",
                "0:a",
                "-c",
                "copy",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        )
        has_no_audio = probe.returncode != 0 and "matches no streams" in probe.stderr
        print(
            f"source_bytes={source.stat().st_size} "
            f"result_bytes={result.stat().st_size} audio_streams={0 if has_no_audio else 'unknown'}"
        )
        return 0 if has_no_audio else 1


if __name__ == "__main__":
    raise SystemExit(main())
