from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from funasr import AutoModel

from temp_media import allocate_path, cleanup_success, mark_failed
from control_plane.media_fetch import AUDIO_MEDIA, sniff_media_suffix


MODEL_PATH = os.getenv(
    "EMOTION_VERIFY_MODEL_PATH",
    "/home/donxu/ai-centre/models/.cache/modelscope/models/"
    "iic--emotion2vec_plus_large/snapshots/master",
)
MAX_AUDIO_BYTES = int(os.getenv("EMOTION_VERIFY_MAX_AUDIO_BYTES", str(512 * 1024 * 1024)))

_model: Any = None
_inference_lock = threading.Lock()


def _load_model() -> Any:
    global _model
    if _model is None:
        _model = AutoModel(model=MODEL_PATH, device="cpu", disable_update=True)
    return _model


def _classify(path: Path) -> dict[str, Any]:
    with _inference_lock:
        result = _load_model().generate(
            input=str(path),
            granularity="utterance",
            extract_embedding=False,
            disable_pbar=True,
        )
    if not result:
        raise RuntimeError("emotion model returned no result")
    item = result[0]
    labels = item.get("labels") or item.get("label") or []
    scores = item.get("scores") or item.get("score") or []
    if isinstance(labels, str):
        labels = [labels]
    if isinstance(scores, (int, float)):
        scores = [scores]
    pairs = [
        {"label": str(label), "score": round(float(score), 6)}
        for label, score in zip(labels, scores)
    ]
    pairs.sort(key=lambda pair: pair["score"], reverse=True)
    if not pairs:
        raise RuntimeError("emotion model returned no labels")
    return {"label": pairs[0]["label"], "score": pairs[0]["score"], "scores": pairs}


async def _save_upload(upload: UploadFile) -> Path:
    first_chunk = await upload.read(1024 * 1024)
    if not first_chunk:
        raise HTTPException(status_code=400, detail="audio is empty")
    suffix = sniff_media_suffix(first_chunk[:64])
    if suffix not in AUDIO_MEDIA.suffixes:
        raise HTTPException(status_code=415, detail="unsupported audio file type")
    target = allocate_path(upload.filename or "audio.wav", suffix=suffix)
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


app = FastAPI(title="AI Centre emotion verification", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok" if _model is not None else "loading",
        "model": "emotion2vec_plus_large",
        "device": "cpu",
    }


@app.post("/v1/emotion")
async def classify_emotion(audio: UploadFile = File(...)) -> dict[str, Any]:
    path: Path | None = None
    try:
        path = await _save_upload(audio)
        result = await asyncio.to_thread(_classify, path)
    except HTTPException as exc:
        if path is not None:
            mark_failed(path, exc)
        raise
    except Exception as exc:
        if path is not None:
            mark_failed(path, exc)
        raise HTTPException(
            status_code=422,
            detail=f"unable to classify emotion: {type(exc).__name__}",
        ) from exc
    cleanup_success(path)
    return result
