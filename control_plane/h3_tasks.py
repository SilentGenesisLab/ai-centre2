from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from celery.exceptions import Retry

from .celery_app import celery_app
from .config import get_settings
from .h3_scheduler import H3Scheduler
from .h3_store import H3Store
from .h3_workflow import (
    ComfyH3Client,
    H3Cancelled,
    H3WorkerError,
    H3WorkerIncompatible,
    build_prompt_graph,
    concatenate_parts,
    probe_video,
    render_delivery,
    resolve_dimensions,
    split_reference,
    validate_part_durations,
)
from .media_fetch import AUDIO_MEDIA, IMAGE_MEDIA, VIDEO_MEDIA, download_public_media


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upload_result(path: Path, request: dict[str, Any], job_id: str, stage: str) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    data = {
        "external_ref": request.get("external_ref") or job_id,
        "run_id": "",
        "campaign_id": "",
        "project_id": "",
        "stage": stage,
        "actor": "minimax-h3-worker",
    }
    with path.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (path.name, stream, "video/mp4")},
            timeout=httpx.Timeout(settings.h3_upload_timeout_seconds, connect=30),
        )
    response.raise_for_status()
    result_url = str(response.json()["uri"])
    if not result_url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return result_url


def _cached_source(work_dir: Path) -> Path | None:
    return next((path for path in work_dir.glob("reference.*") if path.is_file()), None)


def _check_cancel(store: H3Store, job_id: str) -> None:
    if store.is_cancel_requested(job_id):
        raise H3Cancelled("job was cancelled")


@celery_app.task(
    bind=True,
    name="control_plane.minimax_h3_generate",
    max_retries=None,
    time_limit=608400,
    soft_time_limit=608340,
)
def generate_minimax_h3(self) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    store = H3Store(settings.h3_db_path)
    scheduler = H3Scheduler(settings, store)
    request = store.request(job_id)
    work_dir = settings.h3_work_dir / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        _check_cancel(store, job_id)
        video_urls = list(request.get("reference_video_urls") or ([request["reference_video_url"]] if request.get("reference_video_url") else []))
        image_urls = list(request.get("reference_image_urls") or ([request.get("reference_image_url") or request.get("identity_image_url")] if request.get("reference_image_url") or request.get("identity_image_url") else []))
        audio_urls = list(request.get("reference_audio_urls") or ([request["reference_audio_url"]] if request.get("reference_audio_url") else []))
        if video_urls or image_urls or audio_urls:
            store.update_job(job_id, status="running", stage="downloading", progress=5, started_at=_utc_now())

        def download_many(urls: list[str], stem: str, media, limit: int) -> list[Path]:
            results: list[Path] = []
            for index, url in enumerate(urls):
                prefix = f"{stem}-{index + 1}"
                cached = next((path for path in work_dir.glob(f"{prefix}.*") if path.is_file()), None)
                results.append(cached or download_public_media(str(url), work_dir, prefix, media, limit, settings.h3_download_timeout_seconds).path)
            return results

        reference_videos = download_many(video_urls, "reference-video", VIDEO_MEDIA, settings.h3_max_video_bytes)
        reference_images = download_many(image_urls, "reference-image", IMAGE_MEDIA, settings.h3_max_image_bytes)
        reference_audios = download_many(audio_urls, "reference-audio", AUDIO_MEDIA, settings.h3_max_audio_bytes)
        source: Path | None = reference_videos[0] if reference_videos else _cached_source(work_dir)
        has_audio = False
        additional_video_audio_flags: list[bool] = []
        source_duration: float | None = None
        if source is not None:
            source_duration, has_audio = probe_video(source)
            additional_video_audio_flags = [probe_video(path)[1] for path in reference_videos[1:]]
            reference_durations = validate_part_durations(
                source_duration, str(request["segment_mode"]), request.get("split_seconds")
            )
            durations = (
                [float(request.get("duration_seconds") or source_duration)]
                if request["segment_mode"] == "single"
                else reference_durations
            )
            parts: list[Path | None] = list(split_reference(source, work_dir, reference_durations))
        else:
            durations = [float(request.get("duration_seconds") or 5)]
            parts = [None]
        model_width, model_height, output_width, output_height = resolve_dimensions(
            str(request.get("resolution") or "480p"), str(request.get("aspect_ratio") or "9:16"),
            request.get("width"), request.get("height"), str(request.get("quality") or "medium"),
        )
        quality = "medium" if request.get("quality") == "midia" else str(request.get("quality") or "medium")
        store.update_job(
            job_id, source_duration_seconds=round(source_duration, 3) if source_duration else None,
            stage="waiting_for_worker", progress=15,
            output_metadata={
                "resolution": request.get("resolution") or ("custom" if request.get("width") else "480p"),
                "quality": quality,
                "aspect_ratio": request.get("aspect_ratio") or "9:16",
                "duration_seconds": round(sum(durations), 3),
                "width": output_width, "height": output_height,
                "model_width": model_width, "model_height": model_height,
            },
        )
        store.scrub_request_urls(job_id)

        job_record = store.job(job_id, include_attempts=False)
        created_at = datetime.fromisoformat(str(job_record["created_at"]))
        waited_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
        first_attempt = int(job_record["attempt_count"]) + 1
        for attempt_number in range(first_attempt, 4):
            _check_cancel(store, job_id)
            assigned = scheduler.acquire(job_id, attempt_number)
            if assigned is None:
                queue = store.queue_position(job_id)
                if queue["ahead_count"] == 0:
                    scheduler.probe_all()
                    assigned = scheduler.acquire(job_id, attempt_number)
            if assigned is None:
                if waited_seconds >= settings.h3_wait_for_worker_seconds:
                    raise RuntimeError("no compatible H3 worker became available within 7 days")
                store.update_job(job_id, status="queued", stage="waiting_for_worker", progress=15)
                priority = int(job_record.get("priority") or 500)
                raise self.retry(
                    countdown=settings.h3_health_interval_seconds,
                    max_retries=None,
                    priority=round((priority - 1) * 9 / 999),
                )

            worker, lease_version = assigned
            worker_id = str(worker["id"])
            lease_value = str(worker["lease_value"])
            store.begin_attempt(job_id, worker_id, attempt_number, lease_version)
            output_parts: list[Path] = []
            prompt_ids: list[str] = []
            try:
                with ComfyH3Client(str(worker["base_url"]), timeout_seconds=120) as client:
                    image_names = [client.upload(path, f"{job_id}-reference-image-{index}{path.suffix.lower()}") for index, path in enumerate(reference_images, start=1)]
                    audio_names = [client.upload(path, f"{job_id}-reference-audio-{index}{path.suffix.lower()}") for index, path in enumerate(reference_audios, start=1)]
                    extra_video_names = [client.upload(path, f"{job_id}-reference-extra-{index}.mp4") for index, path in enumerate(reference_videos[1:], start=2)]
                    for index, (part, part_duration, prompt) in enumerate(
                        zip(parts, durations, request["prompts"], strict=True), start=1
                    ):
                        _check_cancel(store, job_id)
                        video_name = (
                            client.upload(part, f"{job_id}-reference-{index}.mp4") if part else None
                        )
                        graph = build_prompt_graph(
                            video_name, str(prompt), part_duration, model_width,
                            model_height, int(request["seed"]) + index - 1,
                            f"video/{job_id}-part-{index}",
                            video_names=([video_name] if video_name else []) + extra_video_names,
                            reference_image_names=image_names,
                            reference_audio_names=audio_names,
                            reference_video_audio_flags=([has_audio] if video_name else []) + additional_video_audio_flags,
                            quality=quality,
                        )
                        prompt_id = client.submit(graph, client_id=f"ai-centre-h3-{job_id}")
                        prompt_ids.append(prompt_id)
                        store.set_attempt_prompt_ids(job_id, attempt_number, prompt_ids)
                        progress = 25 + int((index - 1) / len(parts) * 55)
                        store.update_job(job_id, stage=f"generating_part_{index}", progress=progress)

                        def on_poll() -> None:
                            _check_cancel(store, job_id)
                            if not scheduler.renew(worker_id, lease_value):
                                raise H3WorkerError("worker lease was lost")

                        output = work_dir / f"attempt-{attempt_number}-part-{index}.mp4"
                        client.wait_and_download(
                            prompt_id, output, settings.h3_worker_poll_seconds,
                            settings.h3_worker_timeout_seconds, on_poll,
                        )
                        output_parts.append(output)
                        if index < len(parts):
                            client.free_temporary()

                _check_cancel(store, job_id)
                if not scheduler.renew(worker_id, lease_value):
                    raise H3WorkerError("worker lease expired before result publication")
                store.update_job(job_id, stage="rendering_delivery", progress=80)
                delivery_parts: list[Path] = []
                for index, (output, target_duration) in enumerate(
                    zip(output_parts, durations, strict=True), start=1
                ):
                    delivery = work_dir / f"delivery-part-{index}.mp4"
                    render_delivery(
                        output, delivery, output_width, output_height,
                        duration_seconds=target_duration,
                    )
                    delivery_parts.append(delivery)
                store.update_job(job_id, stage="merging", progress=85)
                final_path = work_dir / "final.mp4"
                concatenate_parts(delivery_parts, final_path)
                store.update_job(job_id, stage="uploading_oss", progress=90)
                part_urls = [
                    _upload_result(path, request, job_id, f"media.minimax_h3.part_{index}")
                    for index, path in enumerate(delivery_parts, start=1)
                ]
                result_url = _upload_result(final_path, request, job_id, "media.minimax_h3.final")
                elapsed = round(time.perf_counter() - started, 3)
                published = store.complete_job_if_active(
                    job_id, attempt_number, lease_version, result_url=result_url,
                    part_urls=part_urls, elapsed_seconds=elapsed,
                    output_metadata={
                        "resolution": request.get("resolution") or ("custom" if request.get("width") else "480p"),
                        "quality": quality,
                        "aspect_ratio": request.get("aspect_ratio") or "9:16",
                        "duration_seconds": round(sum(durations), 3),
                        "width": output_width, "height": output_height,
                        "model_width": model_width, "model_height": model_height,
                    },
                )
                if not published:
                    scheduler.release(worker_id, lease_value)
                    return store.job(job_id)
                scheduler.release(worker_id, lease_value)
                result = store.job(job_id)
                shutil.rmtree(work_dir, ignore_errors=True)
                return result
            except H3Cancelled:
                if prompt_ids:
                    try:
                        with ComfyH3Client(str(worker["base_url"]), timeout_seconds=10) as client:
                            client.cancel(prompt_ids[-1])
                    except Exception:
                        pass
                store.finish_attempt(job_id, attempt_number, "cancelled", "cancelled")
                scheduler.release(worker_id, lease_value)
                raise
            except H3WorkerIncompatible as exc:
                store.record_probe(worker_id, ok=True, compatible=False, error=str(exc))
                store.finish_attempt(job_id, attempt_number, "failed", str(exc))
                scheduler.release(worker_id, lease_value)
            except Exception as exc:
                safe_error = (
                    f"{type(exc).__name__}: {str(exc)}"
                    if isinstance(exc, H3WorkerError)
                    else f"{type(exc).__name__}: H3 worker attempt failed"
                )
                store.finish_attempt(job_id, attempt_number, "failed", safe_error)
                scheduler.release(worker_id, lease_value)
            for output in output_parts:
                output.unlink(missing_ok=True)
            if attempt_number < 3:
                store.update_job(job_id, status="queued", stage="retrying_on_another_worker", progress=15)

        raise RuntimeError("H3 generation failed after 3 attempts")
    except Retry:
        raise
    except H3Cancelled:
        elapsed = round(time.perf_counter() - started, 3)
        store.update_job(
            job_id, status="cancelled", stage="cancelled", error=None,
            finished_at=_utc_now(), elapsed_seconds=elapsed,
        )
        return store.job(job_id)
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        store.update_job(
            job_id, status="failed", stage="failed", error=str(exc)[:500],
            finished_at=_utc_now(), elapsed_seconds=elapsed,
        )
        return store.job(job_id)
