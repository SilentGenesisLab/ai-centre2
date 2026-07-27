from __future__ import annotations

from celery import Celery

from .config import get_settings


settings = get_settings()
celery_app = Celery(
    "face_mosaic_worker",
    broker=settings.redis_broker_url,
    backend=settings.redis_result_url,
    include=["face_worker.tasks"],
)
celery_app.conf.update(
    task_default_queue="face_mosaic",
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    task_time_limit=1800,
    task_soft_time_limit=1740,
)

