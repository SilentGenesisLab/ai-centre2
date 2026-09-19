from __future__ import annotations

import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

try:
    import fcntl
except ImportError:  # Windows-only test environments
    fcntl = None

from .celery_app import celery_app
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media

if TYPE_CHECKING:
    from .depth_inference import VideoDepthInference


_GPU_THREAD_LOCK = threading.Lock()
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE: tuple[tuple[str, str], VideoDepthInference] | None = None


def effective_input_size(
    version: str,
    model_size: str,
    requested: int,
    da2_base_maximum: int,
) -> int:
    if version == "da2" and model_size == "base":
        return min(requested, da2_base_maximum)
    return requested


def _get_model(version: str, model_size: str) -> VideoDepthInference:
    """Keep exactly one model on the GPU and release it before switching."""
    from .depth_inference import DepthAnything3Inference, VideoDepthAnythingInference

    global _MODEL_CACHE
    key = (version, model_size)
    with _MODEL_CACHE_LOCK:
        if _MODEL_CACHE is not None and _MODEL_CACHE[0] == key:
            return _MODEL_CACHE[1]
        if _MODEL_CACHE is not None:
            _MODEL_CACHE[1].close()
            _MODEL_CACHE = None
        settings = get_settings()
        if version == "da2":
            checkpoint = (
                settings.depth_checkpoint_path
                if model_size == "small"
                else settings.depth_da2_base_checkpoint_path
            )
            loaded: VideoDepthInference = VideoDepthAnythingInference(
                settings.depth_source_dir,
                checkpoint,
                model_size,
            )
        elif version == "da3":
            model_dir = (
                settings.depth_da3_small_model_dir
                if model_size == "small"
                else settings.depth_da3_base_model_dir
            )
            loaded = DepthAnything3Inference(
                settings.depth_da3_source_dir,
                model_dir,
                model_size,
                chunk_size=settings.depth_da3_chunk_size,
                overlap=settings.depth_da3_chunk_overlap,
            )
        else:
            raise ValueError(f"unsupported video depth version: {version}")
        _MODEL_CACHE = (key, loaded)
        return loaded


def _upload_result(target: Path, payload: dict[str, Any], job_id: str) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    external_ref = payload.get("external_ref") or job_id
    data = {
        "external_ref": f"{external_ref}:video-depth",
        "run_id": payload.get("run_id") or "",
        "campaign_id": payload.get("campaign_id") or "",
        "project_id": payload.get("project_id") or "",
        "stage": "media.video_depth",
        "actor": "video-depth-worker",
    }
    filename = str(payload.get("filename") or "depth.mp4")
    with target.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (filename, stream, "video/mp4")},
            timeout=httpx.Timeout(settings.depth_upload_timeout_seconds, connect=30),
        )
    response.raise_for_status()
    video_url = str(response.json()["uri"])
    if not video_url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return video_url


@contextmanager
def _gpu_slot(lock_path: Path):
    with _GPU_THREAD_LOCK:
        if fcntl is None:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@celery_app.task(
    bind=True,
    name="control_plane.video_depth",
    time_limit=7200,
    soft_time_limit=7140,
)
def infer_video_depth(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    work_dir = settings.depth_work_dir / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 2})
        source = download_public_media(
            str(request_data["source_uri"]),
            work_dir,
            "source",
            VIDEO_MEDIA,
            settings.depth_max_download_bytes,
            settings.depth_download_timeout_seconds,
        ).path
        target = work_dir / "depth.mp4"

        def update_progress(stage: str, progress: int) -> None:
            self.update_state(
                state="PROGRESS",
                meta={"stage": stage, "progress": progress},
            )

        self.update_state(state="PROGRESS", meta={"stage": "waiting_gpu", "progress": 15})
        with _gpu_slot(settings.depth_gpu_lock_path):
            version = str(request_data["version"])
            model_size = str(request_data["model"])
            requested_input_size = int(request_data["input_size"])
            actual_input_size = effective_input_size(
                version,
                model_size,
                requested_input_size,
                settings.depth_da2_base_max_input_size,
            )
            depth_model = _get_model(version, model_size)
            metrics = depth_model.process(
                source,
                target,
                input_size=actual_input_size,
                max_resolution=int(request_data["max_resolution"]),
                target_fps=float(request_data["target_fps"]),
                progress=update_progress,
            )
        self.update_state(state="PROGRESS", meta={"stage": "uploading", "progress": 95})
        video_url = _upload_result(target, request_data, job_id)
        return {
            "job_id": job_id,
            "status": "succeeded",
            "stage": "completed",
            "version": version,
            "model": model_size,
            "model_name": depth_model.model_name,
            "requested_input_size": requested_input_size,
            "effective_input_size": actual_input_size,
            "input_size_capped": actual_input_size != requested_input_size,
            "video_url": video_url,
            **metrics,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
