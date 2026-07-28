from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .config import get_settings
from .gpu_control import GpuController
from .tts.base import (
    PermanentTTSProviderError,
    TransientTTSProviderError,
    TTSProviderError,
)
from .tts.jobs import TTSJobClient, TTSJobNotFound, TTSJobNotReady
from .tts.runtime import get_tts_service
from .tts.schemas import (
    TTSJobAccepted,
    TTSJobRequest,
    TTSJobStatus,
    TTSSpeechRequest,
    VoiceProfile,
)
from .tts.voices import VoiceProfileNotFound
from .upstreams import AudioUpstreams


app = FastAPI(
    title="AI Centre 2 Control Plane",
    version="2.0.0",
    servers=[
        {
            "url": "http://aicentre2.sligenai.cn:8320",
            "description": "Public domain",
        },
        {
            "url": "http://127.0.0.1:8320",
            "description": "Server-local access",
        },
    ],
)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    reference_wav_path: str | None = None
    prompt_wav_path: str | None = None
    prompt_text: str | None = None
    cfg_value: float = Field(default=2.0, ge=1.0, le=3.0)
    inference_timesteps: int = Field(default=10, ge=1, le=50)


def require_service_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {get_settings().service_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
        )


@lru_cache(maxsize=1)
def get_gpu_controller() -> GpuController:
    settings = get_settings()
    return GpuController(settings.control_runtime_dir / "gpu-state.json")


@lru_cache(maxsize=1)
def get_upstreams() -> AudioUpstreams:
    settings = get_settings()
    return AudioUpstreams(
        settings.asr_backend_url,
        settings.tts_backend_url,
        settings.upstream_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_tts_jobs() -> TTSJobClient:
    return TTSJobClient(get_settings())


@app.get("/health")
async def health() -> dict[str, Any]:
    upstreams = await get_upstreams().health()
    healthy = all(item["status"] == "ok" for item in upstreams.values())
    return {
        "status": "ok" if healthy else "degraded",
        "upstreams": upstreams,
        "gpus": await asyncio.to_thread(get_gpu_controller().all_states),
    }


@app.post(
    "/v1/asr/transcriptions",
    dependencies=[Depends(require_service_token)],
)
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    beam_size: int = Form(default=5, ge=1, le=10),
) -> dict[str, Any]:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty audio file")
    try:
        return await get_upstreams().transcribe(
            file.filename or "audio.wav",
            content,
            language,
            beam_size,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ASR backend failed: {exc}") from exc


@app.post(
    "/v1/tts/speech",
    dependencies=[Depends(require_service_token)],
)
async def synthesize(request: TTSRequest) -> Response:
    try:
        content, media_type, headers = await get_upstreams().synthesize(
            request.model_dump(exclude_none=True)
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS backend failed: {exc}") from exc
    return Response(content=content, media_type=media_type, headers=headers)


@app.post(
    "/v2/tts/speech",
    dependencies=[Depends(require_service_token)],
)
async def synthesize_v2(request: TTSSpeechRequest) -> Response:
    try:
        result = await get_tts_service().synthesize(request)
    except VoiceProfileNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=f"voice profile not found: {exc.args[0]}",
        ) from exc
    except PermanentTTSProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TransientTTSProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TTSProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    headers = {
        "X-TTS-Provider": result.provider,
        "X-Audio-Duration-Ms": str(result.audio_duration_ms),
    }
    if result.provider_request_id:
        headers["X-Provider-Request-Id"] = result.provider_request_id
    return Response(
        content=result.audio,
        media_type="audio/wav",
        headers=headers,
    )


@app.post(
    "/v2/tts/jobs",
    response_model=TTSJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_tts_job(request: TTSJobRequest) -> TTSJobAccepted:
    try:
        return await asyncio.to_thread(get_tts_jobs().submit, request)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue TTS job: {type(exc).__name__}",
        ) from exc


@app.get(
    "/v2/tts/jobs/{job_id}",
    response_model=TTSJobStatus,
    dependencies=[Depends(require_service_token)],
)
async def get_tts_job(job_id: str) -> TTSJobStatus:
    try:
        return await asyncio.to_thread(get_tts_jobs().status, job_id)
    except TTSJobNotFound as exc:
        raise HTTPException(status_code=404, detail="TTS job not found") from exc


@app.get(
    "/v2/tts/jobs/{job_id}/audio",
    dependencies=[Depends(require_service_token)],
)
async def get_tts_job_audio(job_id: str) -> FileResponse:
    try:
        path = await asyncio.to_thread(get_tts_jobs().audio_path, job_id)
    except TTSJobNotFound as exc:
        raise HTTPException(status_code=404, detail="TTS job not found") from exc
    except TTSJobNotReady as exc:
        raise HTTPException(status_code=409, detail="TTS job is not complete") from exc
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"{job_id}.wav",
    )


@app.get(
    "/v2/tts/providers",
    dependencies=[Depends(require_service_token)],
)
async def tts_provider_statuses() -> dict[str, Any]:
    return {"providers": get_tts_service().statuses()}


@app.get(
    "/v2/tts/voices",
    dependencies=[Depends(require_service_token)],
)
async def list_tts_voices() -> dict[str, Any]:
    return {
        "voices": [
            profile.model_dump(mode="json")
            for profile in get_tts_service().registry.list()
        ]
    }


@app.put(
    "/v2/tts/voices/{voice_profile_id}",
    response_model=VoiceProfile,
    dependencies=[Depends(require_service_token)],
)
async def put_tts_voice(
    voice_profile_id: str,
    profile: VoiceProfile,
) -> VoiceProfile:
    if voice_profile_id != profile.voice_profile_id:
        raise HTTPException(
            status_code=400,
            detail="voice_profile_id path and body values must match",
        )
    return await asyncio.to_thread(get_tts_service().registry.put, profile)


@app.get(
    "/v1/admin/gpus",
    dependencies=[Depends(require_service_token)],
)
async def gpu_states() -> dict[str, Any]:
    return {"gpus": await asyncio.to_thread(get_gpu_controller().all_states)}


@app.post(
    "/v1/admin/gpus/{gpu_id}/drain",
    dependencies=[Depends(require_service_token)],
)
async def drain_gpu(gpu_id: int) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_gpu_controller().drain, gpu_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/v1/admin/gpus/{gpu_id}/disable",
    dependencies=[Depends(require_service_token)],
)
async def disable_gpu(gpu_id: int) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_gpu_controller().drain, gpu_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/v1/admin/gpus/{gpu_id}/enable",
    dependencies=[Depends(require_service_token)],
)
async def enable_gpu(gpu_id: int) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_gpu_controller().enable, gpu_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
