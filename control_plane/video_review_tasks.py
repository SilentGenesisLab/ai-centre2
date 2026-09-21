from __future__ import annotations

import logging

from .celery_app import celery_app
from .concurrency import job_slot, slot_wait_reporter
from .config import get_settings
from .video_review_pipeline import run_video_review


# httpx logs request URLs at INFO. Review inputs may contain signed query
# parameters, so keep those URLs out of the worker journal and rely on the
# structured job state for diagnostics instead.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@celery_app.task(bind=True, name="control_plane.video_review", time_limit=7200, soft_time_limit=7140)
def review_video(self, request_data: dict) -> dict:
    settings = get_settings()

    def update(stage: str, progress: int) -> None:
        self.update_state(state="PROGRESS", meta={"stage": stage, "progress": progress})

    with job_slot("video_review", on_wait=slot_wait_reporter(self)):
        return run_video_review(request_data, settings, str(self.request.id), update)
