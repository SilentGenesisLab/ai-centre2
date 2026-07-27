from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import get_settings
from .gpu_control import GpuController
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
