from __future__ import annotations

from typing import Any
from uuid import UUID

from celery.result import AsyncResult
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .celery_app import celery_app
from .config import get_settings
from .tasks import process_face_mosaic


app = FastAPI(title="AI Centre Face Mosaic Worker", version="1.0.0")


class FaceMosaicJobRequest(BaseModel):
    source_uri: str
    filename: str = "face_mosaic.mp4"
    callback_url: str | None = None
    external_ref: str | None = None
    run_id: str | None = None
    campaign_id: str | None = None
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def require_service_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {get_settings().service_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")


def _job_payload(job: AsyncResult) -> dict[str, Any]:
    state = job.state
    if state == "SUCCESS":
        return dict(job.result)
    if state == "FAILURE":
        return {"job_id": job.id, "status": "failed", "error": str(job.result)}
    if state == "PROGRESS":
        return {"job_id": job.id, "status": "running", **(job.info or {})}
    mapping = {"PENDING": "queued", "STARTED": "running", "RETRY": "retrying", "REVOKED": "cancelled"}
    return {"job_id": job.id, "status": mapping.get(state, state.lower())}


@app.get("/health")
def health() -> dict[str, Any]:
    inspection = celery_app.control.inspect(timeout=1)
    active = inspection.active() or {}
    return {
        "status": "ok",
        "redis_broker": get_settings().redis_broker_url.rsplit("/", 1)[-1],
        "workers": sorted(active),
        "worker_count": len(active),
    }


@app.post("/v1/face-mosaic/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_service_token)])
def create_job(request: FaceMosaicJobRequest) -> dict[str, Any]:
    job = process_face_mosaic.apply_async(args=[request.model_dump()], queue="face_mosaic")
    return {
        "job_id": job.id,
        "status": "queued",
        "status_url": f"/v1/face-mosaic/jobs/{job.id}",
    }


@app.get("/v1/face-mosaic/jobs/{job_id}", dependencies=[Depends(require_service_token)])
def get_job(job_id: UUID) -> dict[str, Any]:
    return _job_payload(AsyncResult(str(job_id), app=celery_app))


@app.post("/v1/face-mosaic/jobs/{job_id}/cancel", dependencies=[Depends(require_service_token)])
def cancel_job(job_id: UUID) -> dict[str, Any]:
    celery_app.control.revoke(str(job_id), terminate=False)
    return {"job_id": str(job_id), "status": "cancel_requested"}

