from __future__ import annotations

import random
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import imageio_ffmpeg


ProgressCallback = Callable[[str, int], None]


class VideoWatermarkProcessor:
    def __init__(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        *,
        ffmpeg_bin: str = "auto",
        timeout_seconds: float = 3600,
        keep_intermediates: bool = False,
        retention_seconds: int = 86400,
    ) -> None:
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.output_dir / "temp"
        self.temp_dir.mkdir(exist_ok=True)
        self.ffmpeg_bin = (
            imageio_ffmpeg.get_ffmpeg_exe() if ffmpeg_bin == "auto" else ffmpeg_bin
        )
        self.timeout_seconds = timeout_seconds
        self.keep_intermediates = keep_intermediates
        self.retention_seconds = retention_seconds
        self._intermediate_files: set[Path] = set()
        self.cleanup_expired_intermediates()

    def process(
        self,
        mode: str = "light",
        progress: ProgressCallback | None = None,
    ) -> Path:
        if mode == "light":
            return self.full_process_light(progress)
        if mode == "intensive":
            return self.full_process_intensive(progress)
        raise ValueError(f"unsupported watermark processing mode: {mode}")

    def cleanup_expired_intermediates(self) -> None:
        cutoff = time.time() - self.retention_seconds
        candidates = list(self.temp_dir.iterdir())
        candidates.extend(self.output_dir.glob("step*.mp4"))
        for path in candidates:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()

    def cleanup_current_intermediates(self) -> None:
        if self.keep_intermediates:
            return
        for path in self._intermediate_files:
            path.unlink(missing_ok=True)
        self._intermediate_files.clear()

    def get_video_duration(self) -> float:
        completed = subprocess.run(
            [
                self.ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-i",
                str(self.input_path),
            ],
            capture_output=True,
            check=False,
            timeout=self.timeout_seconds,
        )
        match = re.search(
            rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            completed.stderr,
        )
        if match is None:
            self._raise_process_error("FFmpeg duration probe", completed)
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def generate_white_noise(self, duration: float, sample_rate: int = 44100) -> Path:
        noise_path = self._intermediate_path(
            f"white_noise_{self.input_path.stem}.wav"
        )
        self._run_ffmpeg(
            "white noise generation",
            [
                "-f",
                "lavfi",
                "-i",
                f"anoisesrc=d={duration}:c=white:r={sample_rate}",
                str(noise_path),
            ],
        )
        return noise_path

    def apply_gaussian_noise(self, intensity: int = 4) -> Path:
        output_path = self._intermediate_path(
            f"step1_gaussian_{self.input_path.stem}.mp4"
        )
        self._run_ffmpeg(
            "gaussian noise",
            [
                "-i",
                str(self.input_path),
                "-vf",
                f"noise=alls={intensity}:allf=t+u",
                "-c:a",
                "copy",
                str(output_path),
            ],
        )
        return output_path

    def apply_color_noise(self, input_path: Path, intensity: int = 6) -> Path:
        output_path = self._intermediate_path(
            f"step2_color_{self.input_path.stem}.mp4"
        )
        self._run_ffmpeg(
            "color noise",
            [
                "-i",
                str(input_path),
                "-vf",
                f"noise=c1s={intensity}:c1f=t+u:c2s={intensity}:c2f=t+u",
                "-c:a",
                "copy",
                str(output_path),
            ],
        )
        return output_path

    def apply_geometric_distortion(self, input_path: Path) -> Path:
        output_path = self._intermediate_path(
            f"step3_geo_{self.input_path.stem}.mp4"
        )
        rotation = random.uniform(-0.003, 0.003)
        self._run_ffmpeg(
            "geometric distortion",
            [
                "-i",
                str(input_path),
                "-vf",
                (
                    "scale=trunc(iw*0.97/2)*2:trunc(ih*0.97/2)*2,"
                    f"rotate={rotation}*random(0)"
                ),
                "-c:a",
                "copy",
                str(output_path),
            ],
        )
        return output_path

    def add_audio_noise(self, input_path: Path, noise_level: int = -35) -> Path:
        noise_path = self.generate_white_noise(self.get_video_duration())
        output_path = self._intermediate_path(
            f"step4_audio_{self.input_path.stem}.mp4"
        )
        self._run_ffmpeg(
            "audio noise",
            [
                "-i",
                str(input_path),
                "-i",
                str(noise_path),
                "-filter_complex",
                f"[1:a]volume={noise_level}dB[noise];[0:a][noise]amix=inputs=2",
                "-c:v",
                "copy",
                str(output_path),
            ],
        )
        return output_path

    def apply_frame_interpolation(self, input_path: Path) -> Path:
        output_path = self._intermediate_path(
            f"step5_frame_{self.input_path.stem}.mp4"
        )
        self._run_ffmpeg(
            "frame interpolation",
            [
                "-i",
                str(input_path),
                "-vf",
                "framerate=30:interp_start=0:interp_end=1:scene=100",
                str(output_path),
            ],
        )
        return output_path

    def transcode_hevc(
        self,
        input_path: Path,
        bitrate_variation: float = 10,
    ) -> Path:
        output_path = self._intermediate_path(
            f"step6_hevc_{self.input_path.stem}.mp4"
        )
        base_bitrate = 2000
        variation = random.uniform(-bitrate_variation, bitrate_variation)
        actual_bitrate = base_bitrate * (1 + variation / 100)
        self._run_ffmpeg(
            "HEVC transcode",
            [
                "-i",
                str(input_path),
                "-c:v",
                "libx265",
                "-b:v",
                f"{actual_bitrate}k",
                "-minrate",
                f"{actual_bitrate * 0.9}k",
                "-maxrate",
                f"{actual_bitrate * 1.1}k",
                "-bufsize",
                f"{actual_bitrate * 2}k",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(output_path),
            ],
        )
        return output_path

    def transcode_h264(self, input_path: Path) -> Path:
        output_path = self.output_dir / f"{uuid4().hex}-final_h264_{self.input_path.stem}.mp4"
        self._run_ffmpeg(
            "H.264 transcode",
            [
                "-i",
                str(input_path),
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-preset",
                "medium",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(output_path),
            ],
        )
        return output_path

    def full_process_light(
        self,
        progress: ProgressCallback | None = None,
    ) -> Path:
        try:
            self._notify(progress, "gaussian_noise", 10)
            result = self.apply_gaussian_noise(intensity=4)
            self._notify(progress, "audio_noise", 55)
            result = self.add_audio_noise(result, noise_level=-35)
            final_output = self.output_dir / f"{uuid4().hex}-final_light_{self.input_path.stem}.mp4"
            result.replace(final_output)
            self._intermediate_files.discard(result)
            self._notify(progress, "complete", 100)
            return final_output
        finally:
            self.cleanup_current_intermediates()

    def full_process_intensive(
        self,
        progress: ProgressCallback | None = None,
    ) -> Path:
        try:
            self._notify(progress, "gaussian_noise", 5)
            result = self.apply_gaussian_noise(intensity=5)
            self._notify(progress, "color_noise", 18)
            result = self.apply_color_noise(result)
            self._notify(progress, "geometric_distortion", 31)
            result = self.apply_geometric_distortion(result)
            self._notify(progress, "audio_noise", 44)
            result = self.add_audio_noise(result, noise_level=-38)
            self._notify(progress, "frame_interpolation", 57)
            result = self.apply_frame_interpolation(result)
            self._notify(progress, "hevc_transcode", 70)
            result = self.transcode_hevc(result, bitrate_variation=10)
            self._notify(progress, "h264_transcode", 88)
            final_output = self.transcode_h264(result)
            self._notify(progress, "complete", 100)
            return final_output
        finally:
            self.cleanup_current_intermediates()

    def _intermediate_path(self, filename: str) -> Path:
        self.temp_dir.mkdir(exist_ok=True)
        path = self.temp_dir / f"{uuid4().hex}-{filename}"
        self._intermediate_files.add(path)
        return path

    def _run_ffmpeg(self, stage: str, arguments: list[str]) -> None:
        completed = subprocess.run(
            [
                self.ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *arguments,
            ],
            capture_output=True,
            check=False,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            self._raise_process_error(f"FFmpeg {stage}", completed)

    @staticmethod
    def _raise_process_error(stage: str, completed: subprocess.CompletedProcess) -> None:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-1000:]
        raise RuntimeError(f"{stage} failed: {detail or 'unknown error'}")

    @staticmethod
    def _notify(
        callback: ProgressCallback | None,
        stage: str,
        progress: int,
    ) -> None:
        if callback is not None:
            callback(stage, progress)
