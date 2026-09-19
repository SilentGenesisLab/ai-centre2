from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import redis
from celery.result import AsyncResult

from .config import Settings


TASK_NAME = "control_plane.video_review"
QUEUE_NAME = "video_review"


class VideoReviewJobNotFound(KeyError):
    pass


class VideoReviewJobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(settings.redis_result_url, decode_responses=True)

    def submit(self, request_data: dict[str, Any], priority: int = 5) -> AsyncResult:
        job_id = str(uuid4())
        self.redis.set(
            self._marker(job_id),
            json.dumps({"created_at": request_data.get("created_at")}, separators=(",", ":")),
            ex=self.settings.video_review_result_expires_seconds,
        )
        try:
            return self._celery_app().send_task(
                TASK_NAME,
                kwargs={"request_data": request_data},
                task_id=job_id,
                queue=QUEUE_NAME,
                priority=max(0, min(10, int(priority))),
            )
        except Exception:
            self.redis.delete(self._marker(job_id))
            raise

    def status(self, job_id: str) -> dict[str, Any]:
        self._require_job(job_id)
        job = AsyncResult(job_id, app=self._celery_app())
        if job.state == "SUCCESS":
            result = dict(job.result)
            result.setdefault("report_url", f"/v1/video-reviews/jobs/{job_id}/report")
            return result
        if job.state == "FAILURE":
            return {"job_id": job_id, "status": "failed", "stage": "failed", "error": "video review failed"}
        if job.state == "PROGRESS":
            return {"job_id": job_id, "status": "running", **(job.info or {})}
        return {
            "job_id": job_id,
            "status": {
                "PENDING": "queued",
                "RECEIVED": "queued",
                "STARTED": "running",
                "RETRY": "retrying",
                "REVOKED": "cancelled",
            }.get(job.state, job.state.lower()),
        }

    def cancel(self, job_id: str) -> dict[str, str]:
        self._require_job(job_id)
        self._celery_app().control.revoke(job_id, terminate=False)
        return {"job_id": job_id, "status": "cancel_requested"}

    def _require_job(self, job_id: str) -> None:
        if not self.redis.exists(self._marker(job_id)):
            raise VideoReviewJobNotFound(job_id)

    @staticmethod
    def _marker(job_id: str) -> str:
        return f"ai-centre2:video-review-job:{job_id}"

    @staticmethod
    def _celery_app():
        from .celery_app import celery_app

        return celery_app
