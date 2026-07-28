from __future__ import annotations

from celery import Celery

from .config import get_settings


settings = get_settings()

celery_app = Celery(
    "ai-centre2",
    broker=settings.redis_broker_url,
    backend=settings.redis_result_url,
    include=["control_plane.tts.tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    result_accept_content=["json"],
    result_expires=settings.tts_result_expires_seconds,
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    task_track_started=True,
    worker_prefetch_multiplier=1,
)

