from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from funasr import AutoModel

from temp_media import allocate_path, cleanup_success, mark_failed
from control_plane.media_fetch import AUDIO_MEDIA, sniff_media_suffix


MODEL_PATH = os.getenv(
    "SPEAKER_VERIFY_MODEL_PATH",
    "/home/donxu/ai-centre/models/.cache/modelscope/models/"
    "iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master",
)
SIMILARITY_THRESHOLD = float(os.getenv("SPEAKER_VERIFY_THRESHOLD", "0.31"))
MODEL_NAME = os.getenv("SPEAKER_VERIFY_MODEL_NAME", "CAM++")
MAX_AUDIO_BYTES = int(os.getenv("SPEAKER_VERIFY_MAX_AUDIO_BYTES", str(512 * 1024 * 1024)))
REFERENCE_EMBEDDING_CACHE_SIZE = int(
    os.getenv("SPEAKER_VERIFY_REFERENCE_CACHE_SIZE", "128")
)

_model: Any = None
_inference_lock = threading.Lock()
_reference_embeddings: OrderedDict[str, torch.Tensor] = OrderedDict()


def _load_model() -> Any:
    global _model
    if _model is None:
        _model = AutoModel(
            model=MODEL_PATH,
            device="cpu",
            disable_update=True,
        )
    return _model


def _embedding(path: Path) -> torch.Tensor:
    result = _load_model().generate(input=str(path), disable_pbar=True)
    if not result or "spk_embedding" not in result[0]:
        raise RuntimeError("speaker model returned no embedding")
    return result[0]["spk_embedding"].detach().cpu().flatten()


def _similarity(reference: Path, candidate: Path) -> float:
    reference_hash = hashlib.sha256(reference.read_bytes()).hexdigest()
    with _inference_lock:
        reference_embedding = _reference_embeddings.get(reference_hash)
        if reference_embedding is None:
            reference_embedding = _embedding(reference)
            _reference_embeddings[reference_hash] = reference_embedding
            while len(_reference_embeddings) > REFERENCE_EMBEDDING_CACHE_SIZE:
                _reference_embeddings.popitem(last=False)
        else:
            _reference_embeddings.move_to_end(reference_hash)
        candidate_embedding = _embedding(candidate)
    return float(
        torch.nn.functional.cosine_similarity(
            reference_embedding.unsqueeze(0),
            candidate_embedding.unsqueeze(0),
        ).item()
    )


async def _save_upload(upload: UploadFile, stem: str) -> Path:
    first_chunk = await upload.read(1024 * 1024)
    if not first_chunk:
        raise HTTPException(status_code=400, detail="audio is empty")
    suffix = sniff_media_suffix(first_chunk[:64])
    if suffix not in AUDIO_MEDIA.suffixes:
        raise HTTPException(status_code=415, detail="unsupported audio file type")
    target = allocate_path(upload.filename or f"{stem}{suffix}", suffix=suffix)
    size = len(first_chunk)
    try:
        with target.open("wb") as output:
            if size > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413, detail="audio is too large")
            output.write(first_chunk)
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="audio is too large")
                output.write(chunk)
    except BaseException as exc:
        mark_failed(target, exc)
        raise
    target.chmod(0o660)
    return target


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(_load_model)
    yield


app = FastAPI(
    title="AI Centre speaker verification",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if _model is not None else "loading",
        "model": MODEL_NAME,
        "device": "cpu",
        "threshold": SIMILARITY_THRESHOLD,
        "reference_cache_entries": len(_reference_embeddings),
        "reference_cache_capacity": REFERENCE_EMBEDDING_CACHE_SIZE,
    }


@app.post("/v1/speaker-similarity")
async def speaker_similarity(
    reference: UploadFile = File(...),
    candidate: UploadFile = File(...),
) -> dict[str, Any]:
    reference_path: Path | None = None
    candidate_path: Path | None = None
    try:
        reference_path = await _save_upload(reference, "reference")
        candidate_path = await _save_upload(candidate, "candidate")
        similarity = await asyncio.to_thread(
            _similarity,
            reference_path,
            candidate_path,
        )
    except HTTPException as exc:
        if reference_path is not None:
            mark_failed(reference_path, exc)
        if candidate_path is not None:
            mark_failed(candidate_path, exc)
        raise
    except Exception as exc:
        if reference_path is not None:
            mark_failed(reference_path, exc)
        if candidate_path is not None:
            mark_failed(candidate_path, exc)
        raise HTTPException(
            status_code=422,
            detail=f"unable to compare speakers: {type(exc).__name__}",
        ) from exc
    cleanup_success(reference_path, candidate_path)
    return {
        "similarity": round(similarity, 6),
        "same_speaker": similarity >= SIMILARITY_THRESHOLD,
        "threshold": SIMILARITY_THRESHOLD,
    }
