from __future__ import annotations

import io
import subprocess
import wave
from array import array

import imageio_ffmpeg


class AudioNormalizationError(RuntimeError):
    pass


def trim_wav_end(audio: bytes, end_seconds: float, fade_ms: int = 10) -> bytes:
    with wave.open(io.BytesIO(audio), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.readframes(
            min(source.getnframes(), max(1, round(end_seconds * rate)))
        )
    if width == 2:
        samples = array("h")
        samples.frombytes(frames[: len(frames) - len(frames) % 2])
        fade_frames = min(
            len(samples) // max(1, channels),
            round(rate * fade_ms / 1000),
        )
        for frame in range(fade_frames):
            gain = (fade_frames - frame - 1) / max(1, fade_frames)
            for channel in range(channels):
                index = len(samples) - (fade_frames - frame) * channels + channel
                samples[index] = round(samples[index] * gain)
        frames = samples.tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(frames)
    return output.getvalue()


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
            # FFmpeg cannot seek back to finalize the RIFF data size when WAV is
            # written to stdout. Some builds therefore leave a 0xffffffff
            # placeholder in the header, which wave.getnframes() interprets as
            # hours of audio. Reading the available PCM payload gives the real
            # duration for both finalized files and streamed WAV responses.
            pcm = wav_file.readframes(wav_file.getnframes())
            width = wav_file.getsampwidth()
            output_channels = wav_file.getnchannels()
            output_rate = wav_file.getframerate()
            bytes_per_frame = width * output_channels
            if bytes_per_frame <= 0 or output_rate <= 0:
                raise AudioNormalizationError("normalized WAV has invalid parameters")
            duration_ms = round(
                len(pcm)
                * 1000
                / bytes_per_frame
                / output_rate
            )
    except (EOFError, wave.Error) as exc:
        raise AudioNormalizationError("normalized output is not a valid WAV") from exc

    # FFmpeg cannot seek back when it writes WAV to stdout and may leave RIFF
    # and data sizes set to 0xffffffff. Re-wrap the decoded PCM into a seekable
    # buffer so every synchronous response has a finalized, portable header.
    finalized = io.BytesIO()
    with wave.open(finalized, "wb") as wav_file:
        wav_file.setnchannels(output_channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(output_rate)
        wav_file.writeframes(pcm)
    return finalized.getvalue(), duration_ms
