from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import redis
from celery.result import AsyncResult

from ..config import Settings
from .schemas import TTSJobAccepted, TTSJobRequest, TTSJobStatus


TASK_NAME = "control_plane.tts.synthesize"


class TTSJobNotFound(KeyError):
    pass


class TTSJobNotReady(RuntimeError):
    pass


class TTSJobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(
            settings.redis_result_url,
            decode_responses=True,
        )

    def submit(self, request: TTSJobRequest) -> TTSJobAccepted:
        digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
        job_id = f"tts_{digest[:32]}"
        marker = self._marker(job_id)
        created = bool(
            self.redis.set(
                marker,
                request.idempotency_key,
                nx=True,
                ex=self.settings.tts_result_expires_seconds,
            )
        )
        if created:
            try:
                celery_app = _get_celery_app()
                celery_app.send_task(
                    TASK_NAME,
                    kwargs={
                        "request_data": request.model_dump(
                            mode="json",
                            exclude={"idempotency_key"},
                        )
                    },
                    task_id=job_id,
                )
            except Exception:
                self.redis.delete(marker)
                raise
        return TTSJobAccepted(
            job_id=job_id,
            status=self.status(job_id).status,
            duplicate=not created,
        )

    def status(self, job_id: str) -> TTSJobStatus:
        self._require_job(job_id)
        celery_app = _get_celery_app()
        result = AsyncResult(job_id, app=celery_app)
        state = result.state
        status = {
            "PENDING": "queued",
            "RECEIVED": "queued",
            "STARTED": "running",
            "RETRY": "retrying",
            "SUCCESS": "succeeded",
            "FAILURE": "failed",
            "REVOKED": "cancelled",
        }.get(state, state.lower())
        if state == "SUCCESS":
            return TTSJobStatus(job_id=job_id, status=status, result=result.result)
        if state == "FAILURE":
            return TTSJobStatus(
                job_id=job_id,
                status=status,
                error=str(result.result),
            )
        return TTSJobStatus(job_id=job_id, status=status)

    def audio_path(self, job_id: str) -> Path:
        status = self.status(job_id)
        if status.status != "succeeded" or not status.result:
            raise TTSJobNotReady(job_id)
        path = Path(status.result["audio_path"]).resolve()
        output_dir = self.settings.tts_output_dir.resolve()
        if not path.is_relative_to(output_dir):
            raise RuntimeError("job result points outside TTS output directory")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _require_job(self, job_id: str) -> None:
        if not self.redis.exists(self._marker(job_id)):
            raise TTSJobNotFound(job_id)

    @staticmethod
    def _marker(job_id: str) -> str:
        return f"ai-centre2:tts-job:{job_id}"


def _get_celery_app():
    from ..celery_app import celery_app

    return celery_app
