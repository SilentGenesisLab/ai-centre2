from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from ..celery_app import celery_app
from ..config import get_settings
from ..media_fetch import AUDIO_MEDIA, download_public_media_async
from .base import TransientTTSProviderError
from .enhanced import (
    combine_wav_segment_files,
    model_text,
    normalize_wav_silence,
    split_tts_text,
    wav_silence_metrics,
)
from .reference import infer_text_language
from .runtime import get_tts_service
from .schemas import (
    ProsodySpec,
    TimingSpec,
    TTSAsyncSpeechRequest,
    TTSCloneSpeechRequest,
    TTSSpeechRequest,
    TTSProviderName,
)


MAX_REFERENCE_BYTES = 512 * 1024 * 1024
SEGMENT_RETRIES = 3
SEGMENT_CONCURRENCY = 2


@celery_app.task(
    bind=True,
    name="control_plane.tts.synthesize",
    max_retries=2,
    time_limit=21600,
    soft_time_limit=21540,
)
def synthesize_tts(self, request_data: dict[str, Any]) -> dict[str, Any]:
    request = TTSAsyncSpeechRequest.model_validate(request_data)
    settings = get_settings()
    job_id = str(self.request.id)
    work_dir = settings.tts_output_dir / f".{job_id}.work"
    reference_dir = settings.tts_reference_dir / f"async-{job_id}"
    output_path = settings.tts_output_dir / f"{job_id}.wav"
    metadata_path = settings.tts_output_dir / f"{job_id}.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(reference_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        result = asyncio.run(
            _synthesize_long_form(
                self,
                request,
                work_dir,
                reference_dir,
                output_path,
            )
        )
    except TransientTTSProviderError as exc:
        countdown = 2 ** (self.request.retries + 1)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(reference_dir, ignore_errors=True)

    self.update_state(state="PROGRESS", meta={"stage": "uploading", "progress": 95})
    audio_url = _upload_audio(output_path, request, job_id)
    metadata = {
        **result,
        "audio_path": str(output_path),
        "audio_url": audio_url,
        "character_count": len(request.text),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "request_metadata": request.metadata,
    }
    _atomic_write(
        metadata_path,
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return metadata


async def _synthesize_long_form(
    task,
    request: TTSAsyncSpeechRequest,
    work_dir: Path,
    reference_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    # Importing these shared control-plane helpers lazily avoids a module cycle
    # while keeping long-form clone routing identical to the synchronous API.
    from ..api import _prepare_clone_context  # pylint: disable=import-outside-toplevel

    settings = get_settings()
    reference_path: Path | None = None
    if request.reference_audio_url:
        task.update_state(
            state="PROGRESS",
            meta={"stage": "downloading_reference", "progress": 2},
        )
        media = await download_public_media_async(
            request.reference_audio_url,
            reference_dir,
            "reference",
            AUDIO_MEDIA,
            MAX_REFERENCE_BYTES,
            settings.upstream_timeout_seconds,
        )
        reference_path = media.path

    task.update_state(
        state="PROGRESS",
        meta={"stage": "preparing_reference", "progress": 4},
    )
    context = await _prepare_clone_context(
        request,
        reference_path,
        reference_dir if reference_path is not None else None,
    )
    segment_limit = (
        20
        if context.model_reference_path is not None
        and context.model_prompt_text is None
        and infer_text_language(request.text, request.language) == "zh"
        else None
    )
    segments = split_tts_text(request.text, limit=segment_limit)
    if not segments:
        raise ValueError("TTS text did not produce any segments")

    service = get_tts_service()
    semaphore = asyncio.Semaphore(SEGMENT_CONCURRENCY)
    progress_lock = asyncio.Lock()
    completed = 0
    providers: list[str] = [""] * len(segments)
    provider_request_ids: list[str | None] = [None] * len(segments)
    segment_paths = [work_dir / f"segment-{index:05d}.wav" for index in range(len(segments))]

    async def generate(index: int) -> None:
        nonlocal completed
        segment = segments[index]
        common = request.model_dump(exclude={"reference_audio_url", "prompt_text"})
        for key in (
            "emotion",
            "emotion_enhance",
            "quality_mode",
            "clone_mode",
            "emotion_strategy",
        ):
            common.pop(key, None)
        common.update(
            {
                "text": model_text(
                    segment.text,
                    context.style,
                    cloning=(
                        context.model_reference_path is not None
                        and not bool(context.style)
                    ),
                ),
                "prosody": ProsodySpec(),
                "timing": TimingSpec(),
                "seed": request.seed if request.seed is not None else 42,
            }
        )
        if context.model_reference_path is not None:
            internal: TTSSpeechRequest = TTSCloneSpeechRequest(
                **{**common, "provider": TTSProviderName.VOXCPM2},
                reference_audio_path=context.model_reference_path,
                prompt_text=context.model_prompt_text,
            )
        else:
            internal = TTSSpeechRequest(**common)

        base_seed = request.seed if request.seed is not None else 42
        async with semaphore:
            for attempt in range(SEGMENT_RETRIES):
                try:
                    generated = await service.synthesize(
                        internal.model_copy(update={"seed": base_seed + attempt})
                    )
                except TransientTTSProviderError:
                    if attempt + 1 >= SEGMENT_RETRIES:
                        raise
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                silence = wav_silence_metrics(generated.audio)
                if (
                    silence["internal_silence_anomaly"]
                    and attempt + 1 < SEGMENT_RETRIES
                ):
                    continue
                break
        prepared_audio = normalize_wav_silence(generated.audio)
        await asyncio.to_thread(_atomic_write, segment_paths[index], prepared_audio)
        providers[index] = generated.provider
        provider_request_ids[index] = generated.provider_request_id
        async with progress_lock:
            completed += 1
            progress = 5 + round(completed / len(segments) * 82)
            task.update_state(
                state="PROGRESS",
                meta={
                    "stage": "synthesizing",
                    "progress": min(progress, 87),
                    "completed_segments": completed,
                    "total_segments": len(segments),
                },
            )

    await asyncio.gather(*(generate(index) for index in range(len(segments))))
    task.update_state(
        state="PROGRESS",
        meta={"stage": "combining", "progress": 90, "total_segments": len(segments)},
    )
    combined_path = work_dir / "combined.wav"
    combined = await asyncio.to_thread(
        combine_wav_segment_files,
        segment_paths,
        segments,
        combined_path,
    )
    await asyncio.to_thread(
        _finalize_audio,
        combined_path,
        output_path,
        request.prosody,
        settings.tts_ffmpeg_bin,
    )
    with wave.open(str(output_path), "rb") as audio:
        final_duration_ms = round(audio.getnframes() * 1000 / audio.getframerate())

    return {
        "provider": providers[0],
        "providers": sorted(set(providers)),
        "provider_request_ids": [item for item in provider_request_ids if item],
        "audio_duration_ms": final_duration_ms,
        "segment_count": len(segments),
        "sample_rate": combined["sample_rate"],
        "channels": combined["channels"],
        "quality_mode": request.quality_mode.value,
        "quality_status": "standard_non_blocking",
        "clone": {
            "mode": context.effective_mode.value if context.effective_mode else "none",
            "fallback": context.fallback,
            "reference_language": context.reference_language,
            "target_language": context.target_language,
            "emotion_strategy": context.emotion_strategy.value,
            "emotion_enhancement": context.enhancement_status,
        },
    }


def _finalize_audio(
    source: Path,
    target: Path,
    prosody: ProsodySpec,
    ffmpeg_bin: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp.wav")
    try:
        if prosody.speed == prosody.volume == prosody.pitch == 1.0:
            shutil.copyfile(source, temporary)
        else:
            executable = (
                imageio_ffmpeg.get_ffmpeg_exe()
                if ffmpeg_bin == "auto"
                else ffmpeg_bin
            )
            filters = (
                f"rubberband=tempo={prosody.speed:.6f}:pitch={prosody.pitch:.6f},"
                f"volume={prosody.volume:.6f}"
            )
            completed = subprocess.run(
                [
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-af",
                    filters,
                    "-c:a",
                    "pcm_s16le",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                timeout=7200,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"long-form TTS prosody processing failed: {completed.stderr[-1000:]}"
                )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _upload_audio(
    target: Path,
    request: TTSAsyncSpeechRequest,
    job_id: str,
) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    data = {
        "external_ref": request.metadata.get("external_ref") or job_id,
        "run_id": request.metadata.get("run_id") or "",
        "campaign_id": request.metadata.get("campaign_id") or "",
        "project_id": request.metadata.get("project_id") or "",
        "stage": "media.tts.long_form",
        "actor": "tts-worker",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with target.open("rb") as stream:
                response = httpx.post(
                    settings.kernel_upload_url,
                    headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
                    data=data,
                    files={"file": (f"{job_id}.wav", stream, "audio/wav")},
                    timeout=httpx.Timeout(
                        settings.tts_upload_timeout_seconds,
                        connect=30,
                    ),
                )
            response.raise_for_status()
            result_url = str(response.json()["uri"])
            if not result_url.startswith("https://"):
                raise RuntimeError("kernel upload did not return an HTTPS URL")
            return result_url
        except (httpx.HTTPError, KeyError, OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError("long-form TTS OSS upload failed") from last_error


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
