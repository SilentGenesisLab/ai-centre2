from __future__ import annotations

import io
import math
import wave
from array import array
from collections.abc import Iterable


REFERENCE_SAMPLE_RATE = 16_000
REFERENCE_CHANNELS = 1
REFERENCE_WINDOW_SECONDS = 5.0
REFERENCE_MIN_SPEECH_SECONDS = 3.5
REFERENCE_MAX_SPEECH_SECONDS = 5.5
REFERENCE_TAIL_SILENCE_SECONDS = 0.5


def normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    language = value.strip().lower().replace("_", "-")
    aliases = {
        "cmn": "zh",
        "yue": "zh",
        "zh-cn": "zh",
        "zh-tw": "zh",
        "eng": "en",
        "spa": "es",
        "jpn": "ja",
        "kor": "ko",
    }
    return aliases.get(language, language.split("-", 1)[0])


def infer_text_language(text: str, requested: str = "auto") -> str | None:
    requested_language = normalize_language(requested)
    if requested_language and requested_language != "auto":
        return requested_language
    if any("\u3040" <= character <= "\u30ff" for character in text):
        return "ja"
    if any("\uac00" <= character <= "\ud7af" for character in text):
        return "ko"
    if any("\u3400" <= character <= "\u9fff" for character in text):
        return "zh"
    if any("\u0400" <= character <= "\u04ff" for character in text):
        return "ru"
    if any("\u0600" <= character <= "\u06ff" for character in text):
        return "ar"
    if any("\u0590" <= character <= "\u05ff" for character in text):
        return "he"
    if any("\u0900" <= character <= "\u097f" for character in text):
        return "hi"
    if any("\u0e00" <= character <= "\u0e7f" for character in text):
        return "th"
    lowered = f" {text.lower()} "
    if any(character in lowered for character in "áéíóúñü¿¡") or any(
        word in lowered
        for word in (
            " el ", " la ", " los ", " las ", " que ", " una ", " para ",
            " hola ", " mundo ", " gracias ", " buenos ",
        )
    ):
        return "es"
    if any(character in lowered for character in "àèéìíîòóù") or any(
        word in lowered
        for word in (" il ", " lo ", " gli ", " che ", " una ", " per ", " con ")
    ):
        return "it"
    if any("a" <= character <= "z" for character in lowered):
        return "en"
    return None


def languages_differ(reference: str | None, target: str | None) -> bool:
    left = normalize_language(reference)
    right = normalize_language(target)
    return bool(left and right and left != right)


def wav_duration_seconds(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as source:
        frames = source.readframes(source.getnframes())
        bytes_per_frame = source.getsampwidth() * source.getnchannels()
        return len(frames) / bytes_per_frame / source.getframerate()


def slice_wav(audio: bytes, start_seconds: float, duration_seconds: float) -> bytes:
    with wave.open(io.BytesIO(audio), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        source.setpos(min(source.getnframes(), max(0, round(start_seconds * rate))))
        frames = source.readframes(max(1, round(duration_seconds * rate)))
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(frames)
    return output.getvalue()


def append_wav_silence(audio: bytes, seconds: float) -> bytes:
    with wave.open(io.BytesIO(audio), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    frames += b"\0" * round(max(0.0, seconds) * rate) * channels * width
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(frames)
    return output.getvalue()


def wav_speech_segments(
    audio: bytes,
    *,
    frame_seconds: float = 0.02,
    merge_gap_seconds: float = 0.20,
) -> list[tuple[float, float]]:
    """Return energy-VAD speech runs for normalized 16-bit PCM WAV audio."""
    with wave.open(io.BytesIO(audio), "rb") as source:
        if source.getsampwidth() != 2:
            return []
        rate = source.getframerate()
        channels = source.getnchannels()
        samples = array("h")
        samples.frombytes(source.readframes(source.getnframes()))
    if not samples or rate <= 0 or channels <= 0:
        return []

    frame_samples = max(channels, round(rate * frame_seconds) * channels)
    levels: list[float] = []
    for offset in range(0, len(samples), frame_samples):
        frame = samples[offset:offset + frame_samples]
        if not frame:
            continue
        rms = math.sqrt(sum(sample * sample for sample in frame) / len(frame))
        levels.append(20.0 * math.log10(max(rms / 32768.0, 1e-8)))
    if not levels or max(levels) < -55.0:
        return []

    ordered = sorted(levels)
    noise_floor = ordered[min(len(ordered) - 1, len(ordered) // 5)]
    threshold = max(-45.0, min(max(levels) - 12.0, noise_floor + 10.0))
    active = [level >= threshold for level in levels]
    gap_frames = max(1, round(merge_gap_seconds / frame_seconds))
    for index, enabled in enumerate(active):
        if enabled:
            continue
        left = max(0, index - gap_frames)
        right = min(len(active), index + gap_frames + 1)
        if any(active[left:index]) and any(active[index + 1:right]):
            active[index] = True

    segments: list[tuple[float, float]] = []
    start: int | None = None
    for index, enabled in enumerate(active + [False]):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            if (index - start) * frame_seconds >= 0.10:
                segments.append((start * frame_seconds, index * frame_seconds))
            start = None
    return segments


def reference_window_ranges(
    duration_seconds: float,
    speech_segments: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Choose 3.5-5.5 second speech spans bounded by detected pauses."""
    segments = [
        (max(0.0, start), min(duration_seconds, end))
        for start, end in speech_segments
        if end > start
    ]
    natural: list[tuple[float, float]] = []
    for index, (start, _) in enumerate(segments):
        for _, end in segments[index:]:
            span = end - start
            if span > REFERENCE_MAX_SPEECH_SECONDS:
                break
            if span >= REFERENCE_MIN_SPEECH_SECONDS:
                natural.append((round(start, 1), round(span, 1)))

    fallback_duration = min(REFERENCE_WINDOW_SECONDS, duration_seconds)
    center = max(0.0, duration_seconds - REFERENCE_WINDOW_SECONDS) / 2.0
    fallback = [
        (start, round(fallback_duration, 1))
        for start in candidate_window_starts(
            duration_seconds,
            segments,
            fallback_duration,
        )
    ]
    candidates = sorted(
        natural,
        key=lambda item: (abs(item[1] - REFERENCE_WINDOW_SECONDS), abs(item[0] - center)),
    ) + fallback
    unique: list[tuple[float, float]] = []
    for candidate in candidates:
        if candidate[1] <= 0:
            continue
        if all(
            abs(candidate[0] - existing[0]) >= 0.25
            or abs(candidate[1] - existing[1]) >= 0.25
            for existing in unique
        ):
            unique.append(candidate)
        if len(unique) == 5:
            break
    return unique or [(0.0, max(0.1, round(fallback_duration, 1)))]


def candidate_window_starts(
    duration_seconds: float,
    speech_segments: Iterable[tuple[float, float]],
    window_seconds: float = REFERENCE_WINDOW_SECONDS,
) -> list[float]:
    if duration_seconds <= window_seconds + 1.0:
        return [0.0]
    maximum = max(0.0, duration_seconds - window_seconds)
    starts = [maximum / 2.0, 0.0, maximum]
    for start, end in speech_segments:
        center = (max(0.0, start) + min(duration_seconds, end)) / 2.0
        starts.append(min(maximum, max(0.0, center - window_seconds / 2.0)))
    unique: list[float] = []
    for start in starts:
        # VoxCPM2 is unexpectedly sensitive to millisecond-level cut-point
        # drift. Snap windows to a stable 100 ms grid so repeated downloads of
        # the same source do not produce materially different voices.
        rounded = round(start, 1)
        if all(abs(rounded - existing) >= 0.25 for existing in unique):
            unique.append(rounded)
        if len(unique) == 5:
            break
    return unique


def speech_coverage(
    start_seconds: float,
    end_seconds: float,
    speech_segments: Iterable[tuple[float, float]],
) -> float:
    overlaps: list[tuple[float, float]] = []
    for start, end in speech_segments:
        left = max(start_seconds, start)
        right = min(end_seconds, end)
        if right > left:
            overlaps.append((left, right))
    if not overlaps:
        return 1.0
    overlaps.sort()
    merged: list[tuple[float, float]] = []
    for left, right in overlaps:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    speech_seconds = sum(right - left for left, right in merged)
    return min(1.0, speech_seconds / max(0.001, end_seconds - start_seconds))


def unclipped_score(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as source:
        if source.getsampwidth() != 2:
            return 1.0
        samples = array("h")
        samples.frombytes(source.readframes(source.getnframes()))
    if not samples:
        return 0.0
    clipped = sum(abs(sample) >= 32_700 for sample in samples)
    return max(0.0, 1.0 - clipped / len(samples) * 100.0)


def signal_to_noise_db(audio: bytes, frame_seconds: float = 0.02) -> float:
    with wave.open(io.BytesIO(audio), "rb") as source:
        if source.getsampwidth() != 2:
            return 0.0
        rate = source.getframerate()
        channels = source.getnchannels()
        samples = array("h")
        samples.frombytes(source.readframes(source.getnframes()))
    frame_samples = max(channels, round(rate * frame_seconds) * channels)
    levels: list[float] = []
    for offset in range(0, len(samples), frame_samples):
        frame = samples[offset:offset + frame_samples]
        if not frame:
            continue
        rms = math.sqrt(sum(sample * sample for sample in frame) / len(frame))
        levels.append(20.0 * math.log10(max(rms / 32768.0, 1e-8)))
    if not levels:
        return 0.0
    ordered = sorted(levels)
    noise = ordered[min(len(ordered) - 1, len(ordered) // 5)]
    signal = ordered[min(len(ordered) - 1, len(ordered) * 4 // 5)]
    return max(0.0, min(40.0, signal - noise))


def window_score(
    *,
    speaker_similarity: float | None,
    coverage: float,
    unclipped: float,
    start_seconds: float,
    duration_seconds: float,
    window_seconds: float = REFERENCE_WINDOW_SECONDS,
    snr_db: float | None = None,
) -> float:
    maximum = max(0.001, duration_seconds - window_seconds)
    center = maximum / 2.0
    center_score = max(0.0, 1.0 - abs(start_seconds - center) / max(center, 0.001))
    speaker_score = max(0.0, min(1.0, speaker_similarity or 0.0))
    snr_score = max(0.0, min(1.0, (snr_db or 0.0) / 30.0))
    length_score = max(
        0.0,
        1.0 - abs(window_seconds - REFERENCE_WINDOW_SECONDS),
    )
    return (
        speaker_score * 0.30
        + max(0.0, min(1.0, coverage)) * 0.25
        + center_score * 0.15
        + max(0.0, min(1.0, unclipped)) * 0.10
        + snr_score * 0.10
        + length_score * 0.10
    )
