from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import imageio_ffmpeg

from .celery_app import celery_app
from .config import get_settings
from .face_mosaic import FaceMosaicProcessor


_processor: FaceMosaicProcessor | None = None


def _get_processor() -> FaceMosaicProcessor:
    global _processor
    if _processor is None:
        settings = get_settings()
        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
        os.environ["PATH"] = f"{ffmpeg.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        _processor = FaceMosaicProcessor(
            model_path=settings.face_model_path,
            confidence_threshold=settings.face_conf_threshold,
            iou_threshold=settings.face_iou_threshold,
            mosaic_size=settings.face_mosaic_size,
            frame_skip=settings.face_frame_skip,
            intra_op_threads=2,
            ffmpeg_path=ffmpeg,
        )
    return _processor


def _download(source_uri: str, target: Path) -> None:
    settings = get_settings()
    with httpx.stream(
        "GET",
        source_uri,
        follow_redirects=True,
        timeout=httpx.Timeout(settings.download_timeout_sec, connect=30),
    ) as response:
        response.raise_for_status()
        with target.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                output.write(chunk)


def _upload(target: Path, payload: dict[str, Any]) -> str:
    settings = get_settings()
    data = {
        "external_ref": payload.get("external_ref") or payload.get("job_id"),
        "run_id": payload.get("run_id") or "",
        "campaign_id": payload.get("campaign_id") or "",
        "project_id": payload.get("project_id") or "",
        "stage": "media.face_mosaic",
        "actor": "face-mosaic-worker",
    }
    with target.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (payload.get("filename") or "face_mosaic.mp4", stream, "video/mp4")},
            timeout=httpx.Timeout(settings.upload_timeout_sec, connect=30),
        )
    response.raise_for_status()
    result = response.json()
    return str(result["uri"])


def _callback(callback_url: str, result: dict[str, Any]) -> None:
    settings = get_settings()
    try:
        httpx.post(
            callback_url,
            json=result,
            headers={"Authorization": f"Bearer {settings.service_token}"},
            timeout=settings.callback_timeout_sec,
        ).raise_for_status()
    except Exception:
        # The result remains available through the Redis-backed status endpoint.
        pass


@celery_app.task(bind=True, name="face_mosaic.process")
def process_face_mosaic(self, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    work_dir = settings.work_dir / job_id
    source = work_dir / "source.mp4"
    target = work_dir / "face_mosaic.mp4"
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        self.update_state(state="PROGRESS", meta={"phase": "download", "progress": 5})
        _download(str(payload["source_uri"]), source)
        self.update_state(state="PROGRESS", meta={"phase": "detect_and_render", "progress": 15})
        analysis = _get_processor().process_video_file(source, target)
        output = target if analysis.get("applied") else source
        self.update_state(state="PROGRESS", meta={"phase": "upload", "progress": 90})
        video_url = _upload(output, {**payload, "job_id": job_id})
        result = {
            "job_id": job_id,
            "status": "succeeded",
            "video_url": video_url,
            "applied": bool(analysis.get("applied")),
            "analysis": {
                **analysis,
                "worker_host": os.uname().nodename,
                "gpu_visible_devices": os.getenv("CUDA_VISIBLE_DEVICES", ""),
                "elapsed_sec": round(time.perf_counter() - started, 3),
            },
        }
        if payload.get("callback_url"):
            _callback(str(payload["callback_url"]), result)
        return result
    except Exception as exc:
        failure = {
            "job_id": job_id,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
        if payload.get("callback_url"):
            _callback(str(payload["callback_url"]), failure)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

