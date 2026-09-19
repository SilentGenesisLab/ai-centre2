from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import redis
from celery.result import AsyncResult

from .config import Settings


TASK_NAME = "control_plane.video_depth"
QUEUE_NAME = "video_depth"


class DepthJobNotFound(KeyError):
    pass


class DepthJobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(
            settings.redis_result_url,
            decode_responses=True,
        )

    def submit(self, request_data: dict[str, Any], priority: int) -> AsyncResult:
        job_id = str(uuid4())
        marker = self._marker(job_id)
        marker_data = {
            "version": request_data.get("version", "da2"),
            "model": request_data.get("model", "small"),
        }
        self.redis.set(
            marker,
            json.dumps(marker_data, separators=(",", ":")),
            ex=self.settings.depth_result_expires_seconds,
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
        marker = self._require_job(job_id)
        selection = json.loads(marker)
        job = AsyncResult(job_id, app=self._celery_app())
        state = job.state
        if state == "SUCCESS":
            return dict(job.result)
        if state == "FAILURE":
            return {
                "job_id": job_id,
                "status": "failed",
                **selection,
                "error": "video depth inference failed",
            }
        if state == "PROGRESS":
            return {
                "job_id": job_id,
                "status": "running",
                **selection,
                **(job.info or {}),
            }
        status = {
            "PENDING": "queued",
            "RECEIVED": "queued",
            "STARTED": "running",
            "RETRY": "retrying",
            "REVOKED": "cancelled",
        }.get(state, state.lower())
        return {"job_id": job_id, "status": status, **selection}

    def cancel(self, job_id: str) -> dict[str, str]:
        marker = self._require_job(job_id)
        self._celery_app().control.revoke(job_id, terminate=False)
        return {
            "job_id": job_id,
            "status": "cancel_requested",
            **json.loads(marker),
        }

    def _require_job(self, job_id: str) -> str:
        marker = self.redis.get(self._marker(job_id))
        if marker is None:
            raise DepthJobNotFound(job_id)
        # Markers created by the previous release contained only "1".
        if marker == "1":
            return '{"version":"da2","model":"small"}'
        return marker

    @staticmethod
    def _marker(job_id: str) -> str:
        return f"ai-centre2:video-depth-job:{job_id}"

    @staticmethod
    def _celery_app():
        from .celery_app import celery_app

        return celery_app
