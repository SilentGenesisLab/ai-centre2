from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from .config import load_config
from .engine import OcrEngineUnavailable, PaddleOcrEngine, assert_image_paths_exist
from .schemas import (
    BatchRequest,
    BatchResponse,
    SubtitleDetectRequest,
    SubtitleDetectResponse,
)
from .subtitle_detector import detect_subtitle_events


config = load_config()
engine = PaddleOcrEngine(config)
app = FastAPI(title="Video Translate OCR Service", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "config": config.__dict__, "engine": engine.status}


@app.post("/v1/ocr/batch", response_model=BatchResponse)
async def ocr_batch(request: BatchRequest) -> BatchResponse:
    if len(request.images) > config.max_images_per_request:
        raise HTTPException(status_code=413, detail="too many images in one request")
    if any(len(image.regions) > config.max_regions_per_image for image in request.images):
        raise HTTPException(status_code=413, detail="too many regions in one image")
    try:
        assert_image_paths_exist(request.images)
        started = time.perf_counter()
        results = engine.recognize_batch(request.images)
        elapsed = time.perf_counter() - started
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"image not found: {exc}") from exc
    except OcrEngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"ocr engine unavailable: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ocr failed: {exc}") from exc
    response = BatchResponse(
        job_id=request.job_id,
        engine=config.engine,
        model_version=config.model_version,
        device=config.device,
        elapsed_seconds=round(elapsed, 4),
        results=results,
    )
    return response


@app.post("/v1/subtitle-events/detect", response_model=SubtitleDetectResponse)
async def subtitle_events_detect(request: SubtitleDetectRequest) -> SubtitleDetectResponse:
    input_path = Path(request.input_path)
    output_dir = Path(request.output_dir) if request.output_dir else input_path.parent / f"{input_path.stem}_subtitle_events"
    try:
        result = detect_subtitle_events(
            engine=engine,
            input_path=input_path,
            output_dir=output_dir,
            source_lang_hint=request.source_lang_hint,
            mode=request.mode,
            export_debug_video=request.export_debug_video,
            config=request.config,
            asr_segments=request.asr_segments,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"video not found: {exc}") from exc
    except OcrEngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"ocr engine unavailable: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"subtitle detection failed: {exc}") from exc
    return SubtitleDetectResponse(**result)
