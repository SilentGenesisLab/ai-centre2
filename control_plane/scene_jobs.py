from __future__ import annotations

from typing import Any
from uuid import uuid4

import redis
from celery.result import AsyncResult

from .config import Settings


TASK_NAME = "control_plane.scene_detect"
QUEUE_NAME = "scene_detect"


class SceneJobNotFound(KeyError):
    pass


class SceneJobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(
            settings.redis_result_url,
            decode_responses=True,
        )

    def submit(self, request_data: dict[str, Any], priority: int) -> AsyncResult:
        job_id = str(uuid4())
        marker = self._marker(job_id)
        self.redis.set(
            marker,
            "1",
            ex=self.settings.scene_result_expires_seconds,
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
        self._require_job(job_id)
        job = AsyncResult(job_id, app=self._celery_app())
        state = job.state
        if state == "SUCCESS":
            return dict(job.result)
        if state == "FAILURE":
            return {
                "job_id": job_id,
                "status": "failed",
                "error": "scene detection failed",
            }
        if state == "PROGRESS":
            return {
                "job_id": job_id,
                "status": "running",
                **(job.info or {}),
            }
        status = {
            "PENDING": "queued",
            "RECEIVED": "queued",
            "STARTED": "running",
            "RETRY": "retrying",
            "REVOKED": "cancelled",
        }.get(state, state.lower())
        return {"job_id": job_id, "status": status}

    def cancel(self, job_id: str) -> dict[str, str]:
        self._require_job(job_id)
        self._celery_app().control.revoke(job_id, terminate=False)
        return {"job_id": job_id, "status": "cancel_requested"}

    def _require_job(self, job_id: str) -> None:
        if not self.redis.exists(self._marker(job_id)):
            raise SceneJobNotFound(job_id)

    @staticmethod
    def _marker(job_id: str) -> str:
        return f"ai-centre2:scene-job:{job_id}"

    @staticmethod
    def _celery_app():
        from .celery_app import celery_app

        return celery_app
