from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import redis
from celery.result import AsyncResult

from .config import Settings


TASK_NAME = "control_plane.color_grade"
QUEUE_NAME = "color_grade"


class ColorGradeJobNotFound(KeyError):
    pass


class ColorGradeJobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(settings.redis_result_url, decode_responses=True)

    def submit(self, request_data: dict[str, Any], priority: int) -> AsyncResult:
        job_id = str(uuid4())
        marker = self._marker(job_id)
        self.redis.set(
            marker,
            json.dumps({"strength": request_data["strength"]}, separators=(",", ":")),
            ex=getattr(self.settings, "color_grade_result_expires_seconds", 604800),
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
        marker = self.redis.get(self._marker(job_id))
        if marker is None:
            raise ColorGradeJobNotFound(job_id)
        details = json.loads(marker)
        job = AsyncResult(job_id, app=self._celery_app())
        if job.state == "SUCCESS":
            return dict(job.result)
        if job.state == "FAILURE":
            return {"job_id": job_id, "status": "failed", **details, "error": "color grade failed"}
        if job.state == "PROGRESS":
            return {"job_id": job_id, "status": "running", **details, **(job.info or {})}
        state = {"PENDING": "queued", "RECEIVED": "queued", "STARTED": "running", "RETRY": "retrying", "REVOKED": "cancelled"}.get(job.state, job.state.lower())
        return {"job_id": job_id, "status": state, **details}

    def cancel(self, job_id: str) -> dict[str, str]:
        if not self.redis.exists(self._marker(job_id)):
            raise ColorGradeJobNotFound(job_id)
        self._celery_app().control.revoke(job_id, terminate=False)
        return {"job_id": job_id, "status": "cancel_requested"}

    @staticmethod
    def _marker(job_id: str) -> str:
        return f"ai-centre2:color-grade-job:{job_id}"

    @staticmethod
    def _celery_app():
        from .celery_app import celery_app
        return celery_app
