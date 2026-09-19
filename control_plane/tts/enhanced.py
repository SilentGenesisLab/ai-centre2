from __future__ import annotations

import asyncio
import io
import json
import math
import re
import subprocess
import unicodedata
import wave
from array import array
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from ..config import Settings
from .schemas import ProsodySpec


PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
FADE_MS = 10
SENTENCE_SILENCE_MS = 120
PARAGRAPH_SILENCE_MS = 250
SILENCE_FRAME_MS = 20
SILENCE_THRESHOLD_DBFS = -50.0
SILENCE_ANOMALY_SECONDS = 0.8
MAX_SEGMENT_TRAILING_SILENCE_MS = 200
MAX_INTERNAL_SILENCE_MS = 250
_COMMON_ABBREVIATIONS = frozenset(
    {
        "dr",
        "dra",
        "jr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "profa",
        "sr",
        "sra",
        "srta",
        "st",
        "ud",
        "uds",
    }
)


@dataclass(frozen=True)
class TextSegment:
    text: str
    boundary: str


def _text_limit(text: str) -> int:
    cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
    thai = sum("\u0e00" <= character <= "\u0e7f" for character in text)
    if cjk >= max(1, len(text) // 4):
        return 120
    if thai >= max(1, len(text) // 4):
        return 120
    return 300


def _is_unicode_continuation(character: str) -> bool:
    return (
        unicodedata.category(character).startswith("M")
        or character == "\u200d"
        or "\ufe00" <= character <= "\ufe0f"
        or "\U0001f3fb" <= character <= "\U0001f3ff"
    )


def _cluster_safe_cut(text: str, start: int, desired: int) -> int:
    cut = desired
    while cut > start and cut < len(text):
        if _is_unicode_continuation(text[cut]) or text[cut - 1] == "\u200d":
            cut -= 1
            continue
        break
    if cut > start:
        return cut
    cut = desired
    while cut < len(text) and (
        _is_unicode_continuation(text[cut]) or text[cut - 1] == "\u200d"
    ):
        cut += 1
    return cut


def _hard_split(unit: str, limit: int) -> list[str]:
    if len(unit) <= limit:
        return [unit]
    pieces: list[str] = []
    start = 0
    while len(unit) - start > limit:
        remaining = len(unit) - start
        desired = start + limit
        minimum_tail = max(1, limit // 3)
        if remaining - limit < minimum_tail:
            desired = len(unit) - minimum_tail
        whitespace = max(
            (index + 1 for index in range(start, desired) if unit[index].isspace()),
            default=0,
        )
        punctuation = max(
            (
                index + 1
                for index in range(start, desired)
                if unit[index] in ",，、：:"
            ),
            default=0,
        )
        cut = whitespace or punctuation or desired
        cut = _cluster_safe_cut(unit, start, cut)
        pieces.append(unit[start:cut])
        start = cut
    if start < len(unit):
        pieces.append(unit[start:])
    return pieces


def _period_ends_sentence(text: str, index: int) -> bool:
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if previous.isdigit() and following.isdigit():
        return False
    if following and not following.isspace():
        return False
    word_start = index
    while word_start and text[word_start - 1].isalpha():
        word_start -= 1
    word = text[word_start:index].casefold()
    return len(word) != 1 and word not in _COMMON_ABBREVIATIONS


def _sentence_units(text: str) -> list[str]:
    units: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        boundary = character in "。！？!?；;\n"
        if character == ".":
            boundary = _period_ends_sentence(text, index)
        if not boundary:
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in "。！？!?；;\n":
            end += 1
        units.append(text[start:end])
        start = end
        index = end
    if start < len(text):
        units.append(text[start:])
    return units


def split_tts_text(text: str, limit: int | None = None) -> list[TextSegment]:
    """Split text without dropping or reordering a single character."""
    if not text:
        return []
    limit = limit or _text_limit(text)
    pieces: list[str] = []
    for unit in _sentence_units(text):
        pieces.extend(_hard_split(unit, limit))

    merged: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > limit:
            merged.append(current)
            current = ""
        current += piece
        if len(current) >= limit or "\n" in piece:
            merged.append(current)
            current = ""
    if current:
        merged.append(current)
    return [
        TextSegment(
            text=item,
            boundary="paragraph" if item.endswith("\n") else "sentence",
        )
        for item in merged
        if item
    ]


def _clean_style(value: str) -> str:
    value = re.sub(r"[()（）\r\n]+", "，", value).strip(" ，。")
    return value[:100]


def build_style_instruction(
    emotion: str | None,
    prosody: ProsodySpec,
    *,
    cloning: bool,
) -> str:
    parts: list[str] = []
    if emotion and emotion.strip():
        parts.append(_clean_style(emotion))
    return "，".join(part for part in parts if part)[:200]


def model_text(text: str, style: str, *, cloning: bool = False) -> str:
    text = sanitize_tts_model_text(text)
    if cloning:
        return text
    return f"({style}){text}" if style else text


def sanitize_tts_model_text(text: str) -> str:
    """Remove non-spoken formatting that can cue VoxCPM2 continuation."""
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    cleaned = cleaned.translate(
        str.maketrans("", "", "*`_()[]{}（）【】《》")
    )
    cleaned = re.sub(r"(?<!\w)[\-–—]+|[\-–—]+(?!\w)", " ", cleaned)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


async def enhance_emotion(
    settings: Settings,
    text: str,
    emotion: str | None,
    enabled: bool,
) -> tuple[str | None, str]:
    if not enabled:
        return emotion, "disabled"
    key = (
        settings.doubao_emotion_api_key.get_secret_value()
        if settings.doubao_emotion_api_key
        else ""
    )
    if not key or not settings.doubao_emotion_model:
        return emotion, "fallback"
    requested = emotion.strip() if emotion and emotion.strip() else "由正文自然推断"
    payload = {
        "model": settings.doubao_emotion_model,
        "temperature": 0.2,
        "max_tokens": 180,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是语音表演指导。只输出JSON对象，字段包括emotion_distribution、"
                    "energy、pauses、emphasis和style_instruction。emotion_distribution"
                    "是情绪及0到1权重对象，其余字段用简短中文；style_instruction"
                    "综合前述结构化字段并且不超过100个汉字。"
                    "根据正文和目标情绪生成声音风格指令，"
                    "只描述情绪、节奏、停顿和重音；绝不改写、复述或补充正文。"
                ),
            },
            {
                "role": "user",
                "content": f"目标情绪：{requested}\n正文：{text}",
            },
        ],
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.doubao_emotion_timeout_seconds, connect=3),
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{settings.doubao_emotion_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        style = json.loads(content)["style_instruction"]
        if not isinstance(style, str) or not style.strip():
            raise ValueError("empty emotion instruction")
        return _clean_style(style), "enhanced"
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return emotion, "fallback"


def _wav_pcm(audio: bytes) -> tuple[array, int, int, int]:
    with wave.open(io.BytesIO(audio), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        width = source.getsampwidth()
        if width != 2:
            raise ValueError("only 16-bit WAV is supported")
        samples = array("h")
        samples.frombytes(source.readframes(source.getnframes()))
    return samples, rate, channels, width


def _silent_frame_runs(
    samples: array,
    rate: int,
    channels: int,
    *,
    frame_ms: int = SILENCE_FRAME_MS,
    threshold_dbfs: float = SILENCE_THRESHOLD_DBFS,
) -> tuple[list[tuple[int, int]], int]:
    frame_samples = max(1, round(rate * frame_ms / 1000)) * channels
    flags: list[bool] = []
    for offset in range(0, len(samples), frame_samples):
        frame = samples[offset : offset + frame_samples]
        if not frame:
            continue
        rms = math.sqrt(sum(sample * sample for sample in frame) / len(frame))
        dbfs = 20.0 * math.log10(max(rms / 32768.0, 1e-8))
        flags.append(dbfs <= threshold_dbfs)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, silent in enumerate(flags + [False]):
        if silent and start is None:
            start = index
        elif not silent and start is not None:
            runs.append((start, index))
            start = None
    return runs, frame_samples


def wav_silence_metrics(audio: bytes) -> dict[str, float | bool]:
    samples, rate, channels, _ = _wav_pcm(audio)
    runs, frame_samples = _silent_frame_runs(samples, rate, channels)
    frame_seconds = frame_samples / channels / rate
    frame_count = math.ceil(len(samples) / frame_samples)
    trailing = 0.0
    internal: list[float] = []
    for start, end in runs:
        seconds = (end - start) * frame_seconds
        if end == frame_count:
            trailing = seconds
        elif start != 0:
            internal.append(seconds)
    longest_internal = max(internal, default=0.0)
    return {
        "trailing_silence_seconds": round(trailing, 6),
        "max_internal_silence_seconds": round(longest_internal, 6),
        "silent_tail_anomaly": trailing >= SILENCE_ANOMALY_SECONDS,
        "internal_silence_anomaly": longest_internal >= SILENCE_ANOMALY_SECONDS,
    }


def trim_wav_trailing_silence(
    audio: bytes,
    *,
    keep_ms: int = MAX_SEGMENT_TRAILING_SILENCE_MS,
) -> bytes:
    samples, rate, channels, width = _wav_pcm(audio)
    runs, frame_samples = _silent_frame_runs(samples, rate, channels)
    frame_count = math.ceil(len(samples) / frame_samples)
    if not runs or runs[-1][1] != frame_count:
        return audio
    start, end = runs[-1]
    trailing_seconds = (end - start) * frame_samples / channels / rate
    active_seconds = start * frame_samples / channels / rate
    if trailing_seconds < SILENCE_ANOMALY_SECONDS or active_seconds < 0.25:
        return audio
    keep_samples = round(rate * keep_ms / 1000) * channels
    cut_at = min(len(samples), start * frame_samples + keep_samples)
    trimmed = samples[:cut_at]
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(trimmed.tobytes())
    return output.getvalue()


def normalize_wav_silence(audio: bytes) -> bytes:
    """Shorten generated pseudo-silence while preserving all audible samples."""
    samples, rate, channels, width = _wav_pcm(audio)
    runs, frame_samples = _silent_frame_runs(samples, rate, channels)
    frame_count = math.ceil(len(samples) / frame_samples)
    if not runs:
        return audio
    rebuilt = array("h")
    cursor = 0
    changed = False
    for start, end in runs:
        start_sample = min(len(samples), start * frame_samples)
        end_sample = min(len(samples), end * frame_samples)
        seconds = (end - start) * frame_samples / channels / rate
        is_leading = start == 0
        is_trailing = end == frame_count
        if seconds < SILENCE_ANOMALY_SECONDS or (is_leading and is_trailing):
            continue
        rebuilt.extend(samples[cursor:start_sample])
        keep_ms = (
            MAX_SEGMENT_TRAILING_SILENCE_MS
            if is_leading or is_trailing
            else MAX_INTERNAL_SILENCE_MS
        )
        keep_samples = min(
            end_sample - start_sample,
            round(rate * keep_ms / 1000) * channels,
        )
        if is_leading:
            rebuilt.extend(samples[end_sample - keep_samples : end_sample])
        else:
            rebuilt.extend(samples[start_sample : start_sample + keep_samples])
        cursor = end_sample
        changed = True
    if not changed:
        return audio
    rebuilt.extend(samples[cursor:])
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(rebuilt.tobytes())
    return output.getvalue()


def _fade(samples: array, rate: int, channels: int) -> None:
    frames = min(len(samples) // channels // 2, round(rate * FADE_MS / 1000))
    for frame in range(frames):
        gain_in = frame / max(1, frames)
        gain_out = (frames - frame) / max(1, frames)
        for channel in range(channels):
            first = frame * channels + channel
            last = len(samples) - (frame + 1) * channels + channel
            samples[first] = round(samples[first] * gain_in)
            samples[last] = round(samples[last] * gain_out)


def combine_wav_segments(
    audios: Iterable[bytes],
    segments: list[TextSegment],
) -> bytes:
    combined = array("h")
    rate = PCM_SAMPLE_RATE
    channels = PCM_CHANNELS
    for index, audio in enumerate(audios):
        samples, item_rate, item_channels, _ = _wav_pcm(normalize_wav_silence(audio))
        if index == 0:
            rate = item_rate
            channels = item_channels
        elif item_rate != rate or item_channels != channels:
            raise ValueError("segment WAV format mismatch")
        _fade(samples, rate, channels)
        combined.extend(samples)
        if index < len(segments) - 1:
            pause_ms = (
                PARAGRAPH_SILENCE_MS
                if segments[index].boundary == "paragraph"
                else SENTENCE_SILENCE_MS
            )
            combined.extend([0] * round(rate * pause_ms / 1000) * channels)
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(PCM_SAMPLE_WIDTH)
        target.setframerate(rate)
        target.writeframes(combined.tobytes())
    return output.getvalue()


def combine_wav_segment_files(
    audio_paths: Iterable[Path],
    segments: list[TextSegment],
    target: Path,
) -> dict[str, int]:
    """Combine ordered segment files without retaining the full result in memory."""
    paths = list(audio_paths)
    if not paths or len(paths) != len(segments):
        raise ValueError("audio segment count mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    rate: int | None = None
    channels: int | None = None
    total_frames = 0
    with wave.open(str(target), "wb") as output:
        for index, path in enumerate(paths):
            samples, item_rate, item_channels, width = _wav_pcm(
                normalize_wav_silence(path.read_bytes())
            )
            if width != PCM_SAMPLE_WIDTH:
                raise ValueError("only 16-bit WAV is supported")
            if rate is None:
                rate = item_rate
                channels = item_channels
                output.setnchannels(channels)
                output.setsampwidth(PCM_SAMPLE_WIDTH)
                output.setframerate(rate)
            elif item_rate != rate or item_channels != channels:
                raise ValueError("segment WAV format mismatch")
            assert channels is not None
            _fade(samples, item_rate, item_channels)
            output.writeframesraw(samples.tobytes())
            total_frames += len(samples) // channels
            if index < len(paths) - 1:
                pause_ms = (
                    PARAGRAPH_SILENCE_MS
                    if segments[index].boundary == "paragraph"
                    else SENTENCE_SILENCE_MS
                )
                pause_frames = round(item_rate * pause_ms / 1000)
                output.writeframesraw(
                    array("h", [0]) * (pause_frames * channels)
                )
                total_frames += pause_frames
    assert rate is not None and channels is not None
    return {
        "sample_rate": rate,
        "channels": channels,
        "duration_ms": round(total_frames * 1000 / rate),
    }


def apply_prosody_wav(audio: bytes, ffmpeg_bin: str, prosody: ProsodySpec) -> bytes:
    if prosody.speed == prosody.volume == prosody.pitch == 1.0:
        return audio
    executable = imageio_ffmpeg.get_ffmpeg_exe() if ffmpeg_bin == "auto" else ffmpeg_bin
    filters = (
        f"rubberband=tempo={prosody.speed:.6f}:pitch={prosody.pitch:.6f},"
        f"volume={prosody.volume:.6f}"
    )
    result = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-af",
            filters,
            "-ar",
            str(PCM_SAMPLE_RATE),
            "-ac",
            str(PCM_CHANNELS),
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
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("unable to apply TTS prosody")
    return result.stdout


def wav_duration_seconds(audio: bytes) -> float:
    samples, rate, channels, _ = _wav_pcm(audio)
    return len(samples) / channels / rate


async def process_pcm_stream(
    source: AsyncIterator[bytes],
    ffmpeg_bin: str,
    prosody: ProsodySpec,
) -> AsyncIterator[bytes]:
    executable = imageio_ffmpeg.get_ffmpeg_exe() if ffmpeg_bin == "auto" else ffmpeg_bin
    filters = (
        f"rubberband=tempo={prosody.speed:.6f}:pitch={prosody.pitch:.6f},"
        f"volume={prosody.volume:.6f}"
    )
    process = await asyncio.create_subprocess_exec(
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "-i",
        "pipe:0",
        "-af",
        filters,
        "-f",
        "s16le",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def feed() -> None:
        assert process.stdin is not None
        try:
            async for chunk in source:
                process.stdin.write(chunk)
                await process.stdin.drain()
        finally:
            process.stdin.close()

    feeder = asyncio.create_task(feed())
    try:
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            yield chunk
        await feeder
        if await process.wait() != 0:
            raise RuntimeError("streaming prosody processing failed")
    finally:
        if not feeder.done():
            feeder.cancel()
        if process.returncode is None:
            process.kill()
            await process.wait()


def pcm_silence(milliseconds: int) -> bytes:
    frames = math.ceil(PCM_SAMPLE_RATE * milliseconds / 1000)
    return b"\x00" * frames * PCM_CHANNELS * PCM_SAMPLE_WIDTH
