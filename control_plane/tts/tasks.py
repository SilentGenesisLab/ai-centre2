from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..celery_app import celery_app
from ..config import get_settings
from .base import TransientTTSProviderError
from .runtime import get_tts_service
from .schemas import TTSSpeechRequest


@celery_app.task(
    bind=True,
    name="control_plane.tts.synthesize",
    max_retries=2,
)
def synthesize_tts(self, request_data: dict[str, Any]) -> dict[str, Any]:
    request = TTSSpeechRequest.model_validate(request_data)
    try:
        result = asyncio.run(get_tts_service().synthesize(request))
    except TransientTTSProviderError as exc:
        countdown = 2 ** (self.request.retries + 1)
        raise self.retry(exc=exc, countdown=countdown) from exc

    settings = get_settings()
    output_path = settings.tts_output_dir / f"{self.request.id}.wav"
    metadata_path = settings.tts_output_dir / f"{self.request.id}.json"
    _atomic_write(output_path, result.audio)
    metadata = {
        "provider": result.provider,
        "provider_request_id": result.provider_request_id,
        "audio_duration_ms": result.audio_duration_ms,
        "audio_path": str(output_path),
        "metadata": result.metadata,
        "request_metadata": request.metadata,
    }
    _atomic_write(
        metadata_path,
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return metadata


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

