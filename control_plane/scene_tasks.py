from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import imageio_ffmpeg

from temp_media import allocate_work_directory, cleanup_success, mark_failed

from .celery_app import celery_app
from .concurrency import job_slot, slot_wait_reporter
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media


def _detect_scenes(
    source: Path,
    threshold: float,
    min_scene_len: int,
) -> list[dict[str, float | int]]:
    from scenedetect import ContentDetector, detect

    scene_list = detect(
        str(source),
        ContentDetector(threshold=threshold, min_scene_len=min_scene_len),
        show_progress=False,
        start_in_scene=True,
    )
    return [
        {
            "start_frame": start.get_frames(),
            "end_frame": end.get_frames(),
            "start_seconds": round(start.get_seconds(), 3),
            "end_seconds": round(end.get_seconds(), 3),
            "duration_seconds": round(end.get_seconds() - start.get_seconds(), 3),
        }
        for start, end in scene_list
    ]


def _split_scene(source: Path, target: Path, start: float, duration: float) -> None:
    settings = get_settings()
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-threads",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=settings.scene_ffmpeg_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"FFmpeg scene split failed: {detail}")


def _upload_scene(target: Path, payload: dict[str, Any], index: int) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    external_ref = payload.get("external_ref") or payload["job_id"]
    data = {
        "external_ref": f"{external_ref}:scene:{index:03d}",
        "run_id": payload.get("run_id") or "",
        "campaign_id": payload.get("campaign_id") or "",
        "project_id": payload.get("project_id") or "",
        "stage": "media.scene_detect",
        "actor": "scene-detect-worker",
    }
    filename = f"{Path(payload.get('filename') or 'scene').stem}_{index:03d}.mp4"
    with target.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (filename, stream, "video/mp4")},
            timeout=httpx.Timeout(settings.scene_upload_timeout_seconds, connect=30),
        )
    response.raise_for_status()
    video_url = str(response.json()["uri"])
    if not video_url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return video_url


@celery_app.task(
    bind=True,
    name="control_plane.scene_detect",
    time_limit=7200,
    soft_time_limit=7140,
)
def detect_and_split_scenes(self, request_data: dict[str, Any]) -> dict[str, Any]:
    with job_slot("scene_detect", on_wait=slot_wait_reporter(self)):
        return _detect_and_split_scenes(self, request_data)


def _detect_and_split_scenes(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    work_dir = allocate_work_directory(f"scene-{job_id}", root=settings.temp_data_root)
    started = time.perf_counter()
    failure: BaseException | None = None
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 5})
        source = download_public_media(
            str(request_data["source_uri"]),
            work_dir,
            f"{uuid4().hex}-source",
            VIDEO_MEDIA,
            settings.scene_max_download_bytes,
            settings.scene_download_timeout_seconds,
        ).path
        self.update_state(state="PROGRESS", meta={"stage": "detecting", "progress": 15})
        scenes = _detect_scenes(
            source,
            float(request_data["threshold"]),
            int(request_data["min_scene_len"]),
        )
        if not scenes:
            raise RuntimeError("SceneDetect returned no scenes")

        results: list[dict[str, Any]] = []
        total = len(scenes)
        for index, scene in enumerate(scenes, start=1):
            progress = 20 + int((index - 1) / total * 75)
            self.update_state(
                state="PROGRESS",
                meta={
                    "stage": "splitting_and_uploading",
                    "progress": progress,
                    "scene_index": index,
                    "scene_count": total,
                },
            )
            target = work_dir / f"{uuid4().hex}-scene_{index:03d}.mp4"
            _split_scene(
                source,
                target,
                float(scene["start_seconds"]),
                float(scene["duration_seconds"]),
            )
            video_url = _upload_scene(
                target,
                {**request_data, "job_id": job_id},
                index,
            )
            target.unlink(missing_ok=True)
            results.append({"index": index, **scene, "video_url": video_url})

        return {
            "job_id": job_id,
            "status": "succeeded",
            "scene_count": total,
            "scenes": results,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if failure is None:
            cleanup_success(work_dir)
        else:
            mark_failed(work_dir, failure)
