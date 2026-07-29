from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field


UPSTREAM_URL = os.getenv("VOXCPM2_OPENAI_URL", "http://127.0.0.1:8192").rstrip("/")
ALLOWED_MEDIA_ROOT = Path(
    os.getenv(
        "VOXCPM2_ALLOWED_MEDIA_ROOT",
        "/home/donxu/ai-centre/runtime/references",
    )
)
REQUEST_TIMEOUT_SECONDS = float(os.getenv("VOXCPM2_REQUEST_TIMEOUT_SECONDS", "900"))


class LegacyTTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    reference_wav_path: str | None = None
    prompt_wav_path: str | None = None
    prompt_text: str | None = None
    cfg_value: float = Field(default=2.0, ge=1.0, le=3.0)
    inference_timesteps: int = Field(default=10, ge=1, le=50)


def _reference_uri(raw_path: str, allowed_root: Path) -> str:
    root = allowed_root.expanduser().resolve(strict=True)
    path = Path(raw_path).expanduser().resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError(f"reference audio must be under {root}")
    if not path.is_file():
        raise ValueError("reference audio is not a file")
    return path.as_uri()


def build_openai_payload(
    request: LegacyTTSRequest,
    allowed_root: Path = ALLOWED_MEDIA_ROOT,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input": request.text,
        "voice": "default",
        "response_format": "wav",
    }
    reference_path = request.reference_wav_path or request.prompt_wav_path
    if reference_path:
        payload["ref_audio"] = _reference_uri(reference_path, allowed_root)
        if request.prompt_text:
            payload["ref_text"] = request.prompt_text
    return payload


def validate_sampling(request: LegacyTTSRequest) -> None:
    if request.cfg_value != 2.0 or request.inference_timesteps != 10:
        raise ValueError(
            "managed vLLM-Omni VoxCPM2 supports cfg_value=2.0 and "
            "inference_timesteps=10"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=15),
    )
    yield
    await app.state.client.aclose()


app = FastAPI(
    title="AI Centre VoxCPM2 compatibility gateway",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health(request: Request) -> Response:
    try:
        response = await request.app.state.client.get(f"{UPSTREAM_URL}/health")
        response.raise_for_status()
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "backend": "vllm-omni",
                "error": type(exc).__name__,
            },
        )
    return JSONResponse(
        {
            "status": "ok",
            "backend": "vllm-omni",
            "upstream": UPSTREAM_URL,
        }
    )


async def _synthesize(request: LegacyTTSRequest, raw_request: Request) -> Response:
    try:
        validate_sampling(request)
        payload = build_openai_payload(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        response = await raw_request.app.state.client.post(
            f"{UPSTREAM_URL}/v1/audio/speech",
            json=payload,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"VoxCPM2 upstream unavailable: {type(exc).__name__}",
        ) from exc
    if not response.is_success:
        raise HTTPException(
            status_code=502 if response.status_code >= 500 else 422,
            detail=f"VoxCPM2 upstream returned HTTP {response.status_code}",
        )
    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "audio/wav"),
        headers={
            "X-TTS-Backend": "vllm-omni",
            "X-Legacy-Cfg-Value": str(request.cfg_value),
            "X-Legacy-Inference-Timesteps": str(request.inference_timesteps),
        },
    )


@app.post("/tts")
async def tts(request: LegacyTTSRequest, raw_request: Request) -> Response:
    return await _synthesize(request, raw_request)


@app.post("/clone_path")
async def clone_path(request: LegacyTTSRequest, raw_request: Request) -> Response:
    if not (request.reference_wav_path or request.prompt_wav_path):
        raise HTTPException(status_code=422, detail="reference audio path is required")
    return await _synthesize(request, raw_request)
