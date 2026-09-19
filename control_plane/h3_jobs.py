from __future__ import annotations

from typing import Any
from uuid import uuid4

from celery.result import AsyncResult

from .config import Settings
from .h3_store import H3JobNotFound, H3Store
from .h3_workflow import ComfyH3Client


TASK_NAME = "control_plane.minimax_h3_generate"
QUEUE_NAME = "minimax_h3"


class H3JobClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = H3Store(settings.h3_db_path)

    @staticmethod
    def _broker_priority(priority: int) -> int:
        return round((priority - 1) * 9 / 999)

    def submit(self, request_data: dict[str, Any], priority: int | None = None) -> AsyncResult:
        job_id = str(uuid4())
        selected_priority = int(priority if priority is not None else request_data.get("priority", 500))
        if not 1 <= selected_priority <= 1000:
            raise ValueError("priority must be between 1 and 1000")
        self.store.create_job(job_id, request_data, selected_priority)
        try:
            return self._celery_app().send_task(
                TASK_NAME,
                task_id=job_id,
                queue=QUEUE_NAME,
                priority=self._broker_priority(selected_priority),
            )
        except Exception:
            self.store.update_job(
                job_id,
                status="failed",
                stage="enqueue_failed",
                error="unable to enqueue H3 job",
            )
            raise

    def status(self, job_id: str) -> dict[str, Any]:
        return self.store.job(job_id)

    def queue_position(self, job_id: str) -> dict[str, Any]:
        return self.store.queue_position(job_id)

    def worker_summary(self) -> dict[str, Any]:
        return self.store.worker_summary()

    def cancel(self, job_id: str) -> dict[str, Any]:
        result = self.store.cancel_requested(job_id)
        runtime = self.store.attempt_runtime(job_id)
        if runtime:
            prompt_ids = runtime.get("prompt_ids") or []
            if prompt_ids:
                try:
                    with ComfyH3Client(str(runtime["base_url"]), timeout_seconds=10) as client:
                        client.cancel(str(prompt_ids[-1]))
                except Exception:
                    pass
        else:
            self.store.update_job(job_id, status="cancelled", stage="cancelled")
            result = self.store.job(job_id)
        self._celery_app().control.revoke(job_id, terminate=False)
        return {
            "job_id": job_id,
            "status": result["status"],
            "stage": result["stage"],
        }

    @staticmethod
    def _celery_app():
        from .celery_app import celery_app

        return celery_app


__all__ = ["H3JobClient", "H3JobNotFound"]
