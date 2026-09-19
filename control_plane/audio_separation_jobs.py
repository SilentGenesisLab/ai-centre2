from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import redis
from celery.result import AsyncResult

from .config import Settings


TASK_NAME = "control_plane.audio_separation"
QUEUE_NAME = "audio_separation"


class AudioSeparationJobNotFound(KeyError):
    pass


class AudioSeparationJobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(
            settings.redis_result_url,
            decode_responses=True,
        )

    def submit(self, request_data: dict[str, Any], priority: int) -> AsyncResult:
        job_id = str(uuid4())
        marker = self._marker(job_id)
        marker_data = {"model": request_data["model"]}
        self.redis.set(
            marker,
            json.dumps(marker_data, separators=(",", ":")),
            ex=self.settings.audio_separation_result_expires_seconds,
        )
        try:
            return self._celery_app().send_task(
                TASK_NAME,
                kwargs={"request_data": request_data},
                task_id=job_id,
                queue=QUEUE_NAME,
                priority=priority,
            )
        except Exception:
            self.redis.delete(marker)
            raise

    def status(self, job_id: str) -> dict[str, Any]:
        marker = json.loads(self._require_job(job_id))
        job = AsyncResult(job_id, app=self._celery_app())
        state = job.state
        if state == "SUCCESS":
            return dict(job.result)
        if state == "FAILURE":
            return {
                "job_id": job_id,
                "status": "failed",
                **marker,
                "error": "audio separation failed",
            }
        if state == "PROGRESS":
            return {
                "job_id": job_id,
                "status": "running",
                **marker,
                **(job.info or {}),
            }
        status = {
            "PENDING": "queued",
            "RECEIVED": "queued",
            "STARTED": "running",
            "RETRY": "retrying",
            "REVOKED": "cancelled",
        }.get(state, state.lower())
        return {"job_id": job_id, "status": status, **marker}

    def cancel(self, job_id: str) -> dict[str, str]:
        marker = json.loads(self._require_job(job_id))
        self._celery_app().control.revoke(job_id, terminate=False)
        return {"job_id": job_id, "status": "cancel_requested", **marker}

    def _require_job(self, job_id: str) -> str:
        marker = self.redis.get(self._marker(job_id))
        if marker is None:
            raise AudioSeparationJobNotFound(job_id)
        return marker

    @staticmethod
    def _marker(job_id: str) -> str:
        return f"ai-centre2:audio-separation-job:{job_id}"

    @staticmethod
    def _celery_app():
        from .celery_app import celery_app

        return celery_app
