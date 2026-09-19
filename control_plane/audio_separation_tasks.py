from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

try:
    import fcntl
except ImportError:  # Windows-only test environments
    fcntl = None

from .celery_app import celery_app
from .config import get_settings
from .media_fetch import ASR_MEDIA, download_public_media


_GPU_THREAD_LOCK = threading.Lock()


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


def _prepare_audio(source: Path, target: Path) -> None:
    settings = get_settings()
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=settings.audio_separation_ffmpeg_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
        detail = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"FFmpeg audio extraction failed: {detail}")


def _run_inference(source: Path, output_dir: Path, result_json: Path) -> dict[str, Any]:
    settings = get_settings()
    completed = subprocess.run(
        [
            str(settings.audio_separation_python),
            "-m",
            "control_plane.audio_separation_inference",
            "--input",
            str(source),
            "--output-dir",
            str(output_dir),
            "--source-dir",
            str(settings.audio_separation_source_dir),
            "--checkpoint",
            str(settings.audio_separation_checkpoint_path),
            "--result-json",
            str(result_json),
            "--batch-size",
            str(settings.audio_separation_batch_size),
            "--peak-limit-dbfs",
            str(settings.audio_separation_peak_limit_dbfs),
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=settings.audio_separation_ffmpeg_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0 or not result_json.is_file():
        detail = completed.stderr.strip()[-2000:]
        raise RuntimeError(f"Bandit audio separation failed: {detail}")
    return json.loads(result_json.read_text(encoding="utf-8"))


def _upload_stem(
    target: Path,
    request_data: dict[str, Any],
    job_id: str,
    stem: str,
) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    external_ref = request_data.get("external_ref") or job_id
    data = {
        "external_ref": f"{external_ref}:audio-separation:{stem}",
        "run_id": request_data.get("run_id") or "",
        "campaign_id": request_data.get("campaign_id") or "",
        "project_id": request_data.get("project_id") or "",
        "stage": f"media.audio_separation.{stem}",
        "actor": "audio-separation-worker",
    }
    filename = f"{request_data.get('filename_prefix') or 'separated'}_{stem}.wav"
    with target.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (filename, stream, "audio/wav")},
            timeout=httpx.Timeout(
                settings.audio_separation_upload_timeout_seconds,
                connect=30,
            ),
        )
    response.raise_for_status()
    result_url = str(response.json()["uri"])
    if not result_url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return result_url


@celery_app.task(
    bind=True,
    name="control_plane.audio_separation",
    time_limit=7200,
    soft_time_limit=7140,
)
def separate_audio_task(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    work_dir = settings.audio_separation_work_dir / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 3})
        downloaded = download_public_media(
            str(request_data["source_uri"]),
            work_dir,
            "source",
            ASR_MEDIA,
            settings.audio_separation_max_download_bytes,
            settings.audio_separation_download_timeout_seconds,
        )
        prepared = work_dir / "input.wav"
        self.update_state(state="PROGRESS", meta={"stage": "preparing", "progress": 10})
        _prepare_audio(downloaded.path, prepared)

        self.update_state(state="PROGRESS", meta={"stage": "waiting_gpu", "progress": 15})
        with _gpu_slot(settings.audio_separation_gpu_lock_path):
            self.update_state(state="PROGRESS", meta={"stage": "separating", "progress": 20})
            metrics = _run_inference(
                prepared,
                work_dir / "outputs",
                work_dir / "metrics.json",
            )

        output_paths = {
            stem: Path(path) for stem, path in metrics.pop("output_paths").items()
        }
        result_urls: dict[str, str] = {}
        for index, stem in enumerate(("speech", "music", "sfx", "background"), start=1):
            self.update_state(
                state="PROGRESS",
                meta={
                    "stage": "uploading",
                    "progress": 80 + index * 4,
                    "stem": stem,
                },
            )
            result_urls[f"{stem}_url"] = _upload_stem(
                output_paths[stem], request_data, job_id, stem
            )

        return {
            "job_id": job_id,
            "status": "succeeded",
            "stage": "completed",
            "model": request_data["model"],
            "source_bytes": downloaded.size,
            **result_urls,
            **metrics,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
