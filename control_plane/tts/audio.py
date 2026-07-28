from __future__ import annotations

import io
import subprocess
import wave

import imageio_ffmpeg


class AudioNormalizationError(RuntimeError):
    pass


def normalize_to_wav(
    audio: bytes,
    ffmpeg_bin: str,
    sample_rate: int,
    channels: int,
) -> tuple[bytes, int]:
    if not audio:
        raise AudioNormalizationError("provider returned empty audio")
    executable = (
        imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_bin == "auto"
        else ffmpeg_bin
    )
    process = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ],
        input=audio,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise AudioNormalizationError(f"ffmpeg normalization failed: {message}")
    normalized = process.stdout
    try:
        with wave.open(io.BytesIO(normalized), "rb") as wav_file:
            duration_ms = round(
                wav_file.getnframes() * 1000 / wav_file.getframerate()
            )
    except (EOFError, wave.Error) as exc:
        raise AudioNormalizationError("normalized output is not a valid WAV") from exc
    return normalized, duration_ms
