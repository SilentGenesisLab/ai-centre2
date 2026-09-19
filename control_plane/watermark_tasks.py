from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from .celery_app import celery_app
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media
from .watermark_processor import VideoWatermarkProcessor


def _cleanup_retained_workdirs(root: Path, retention_seconds: int) -> None:
    cutoff = time.time() - retention_seconds
    if not root.is_dir():
        return
    for marker in root.glob("*/.retained"):
        if marker.is_file() and marker.stat().st_mtime < cutoff:
            shutil.rmtree(marker.parent, ignore_errors=True)


def _upload_result(target: Path, payload: dict[str, Any], job_id: str) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    external_ref = payload.get("external_ref") or job_id
    data = {
        "external_ref": f"{external_ref}:watermark-removal",
        "run_id": payload.get("run_id") or "",
        "campaign_id": payload.get("campaign_id") or "",
        "project_id": payload.get("project_id") or "",
        "stage": "media.watermark_remove",
        "actor": "watermark-remove-worker",
    }
    filename = str(payload.get("filename") or "watermark_removed.mp4")
    with target.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (filename, stream, "video/mp4")},
            timeout=httpx.Timeout(settings.watermark_upload_timeout_seconds, connect=30),
        )
    response.raise_for_status()
    video_url = str(response.json()["uri"])
    if not video_url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return video_url


@celery_app.task(
    bind=True,
    name="control_plane.watermark_remove",
    time_limit=7200,
    soft_time_limit=7140,
)
def remove_video_watermark(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    work_root = settings.watermark_work_dir
    _cleanup_retained_workdirs(
        work_root,
        settings.watermark_intermediate_retention_seconds,
    )
    work_dir = work_root / job_id
    output_dir = work_dir / "output"
    work_dir.mkdir(parents=True, exist_ok=True)
    keep_intermediates = bool(request_data.get("keep_intermediates", False))
    started = time.perf_counter()
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 2})
        source = download_public_media(
            str(request_data["source_uri"]),
            work_dir,
            "source",
            VIDEO_MEDIA,
            settings.watermark_max_download_bytes,
            settings.watermark_download_timeout_seconds,
        ).path
        processor = VideoWatermarkProcessor(
            source,
            output_dir,
            ffmpeg_bin=settings.watermark_ffmpeg_bin,
            timeout_seconds=settings.watermark_ffmpeg_timeout_seconds,
            keep_intermediates=keep_intermediates,
            retention_seconds=settings.watermark_intermediate_retention_seconds,
        )

        def update_progress(stage: str, progress: int) -> None:
            self.update_state(
                state="PROGRESS",
                meta={
                    "stage": stage,
                    "progress": 10 + int(progress * 0.78),
                },
            )

        result = processor.process(str(request_data["mode"]), update_progress)
        self.update_state(state="PROGRESS", meta={"stage": "uploading", "progress": 92})
        video_url = _upload_result(result, request_data, job_id)
        return {
            "job_id": job_id,
            "status": "succeeded",
            "mode": request_data["mode"],
            "video_url": video_url,
            "intermediates_retained": keep_intermediates,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        if keep_intermediates:
            for path in work_dir.glob("source.*"):
                path.unlink(missing_ok=True)
            for path in output_dir.glob("final_*.mp4"):
                path.unlink(missing_ok=True)
            (work_dir / ".retained").touch()
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
