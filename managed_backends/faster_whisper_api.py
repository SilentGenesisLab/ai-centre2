from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import BatchedInferencePipeline, WhisperModel


MODEL_NAME = os.getenv("FW_MODEL", "large-v3")
MODEL_DIR = os.getenv("FW_MODEL_DIR")
COMPUTE_TYPE = os.getenv("FW_COMPUTE_TYPE", "float16")
BATCH_SIZE = int(os.getenv("FW_BATCH_SIZE", "8"))
DEVICE = os.getenv("FW_DEVICE", "cuda")
DEVICE_INDEX = int(os.getenv("FW_DEVICE_INDEX", "0"))

app = FastAPI(title="AI Centre 2 faster-whisper", version="2.0.0")
_model: WhisperModel | None = None
_pipeline: BatchedInferencePipeline | None = None
_inference_lock = asyncio.Lock()


def get_pipeline() -> BatchedInferencePipeline:
    global _model, _pipeline
    if _pipeline is None:
        model_source = MODEL_DIR or MODEL_NAME
        _model = WhisperModel(
            model_source,
            device=DEVICE,
            device_index=DEVICE_INDEX,
            compute_type=COMPUTE_TYPE,
        )
        _pipeline = BatchedInferencePipeline(model=_model)
    return _pipeline


@app.on_event("startup")
def preload() -> None:
    get_pipeline()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "device": DEVICE,
        "device_index": DEVICE_INDEX,
        "compute_type": COMPUTE_TYPE,
        "batch_size": BATCH_SIZE,
        "loaded": _pipeline is not None,
    }


@app.post("/asr")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    beam_size: int = Form(default=5, ge=1, le=10),
) -> dict[str, Any]:
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    content = await file.read()
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(content)
            temporary_path = temporary.name
        async with _inference_lock:
            return await asyncio.to_thread(
                _transcribe_sync,
                temporary_path,
                language,
                beam_size,
            )
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def _transcribe_sync(
    audio_path: str,
    language: str | None,
    beam_size: int,
) -> dict[str, Any]:
    segments_iterator, info = get_pipeline().transcribe(
        audio_path,
        language=language,
        beam_size=beam_size,
        batch_size=BATCH_SIZE,
        vad_filter=True,
        word_timestamps=True,
    )
    segments = []
    for segment in segments_iterator:
        segments.append(
            {
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": [
                    {
                        "start": word.start,
                        "end": word.end,
                        "word": word.word,
                        "probability": word.probability,
                    }
                    for word in (segment.words or [])
                ],
            }
        )
    return {
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "segments": segments,
    }

