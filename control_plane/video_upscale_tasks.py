from __future__ import annotations

import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from .celery_app import celery_app
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media


# video_field 随节点类型而变：flashvsr 的视频节点是 LoadVideo（字段 file），
# 另两个是 VHS_LoadVideo（字段 video）。节点号以 RunningHub 应用详情页的
# nodeInfoList 为准，改应用后可能失效。
PROVIDERS: dict[str, dict[str, str]] = {
    "flashvsr": {"app_id": "1996062530516795394", "video_node": "27", "video_field": "file", "size_node": "16"},
    "flashvsr_v2": {"app_id": "1983119055743819777", "video_node": "24", "video_field": "video", "size_node": "16"},
    "seedvr2": {"app_id": "1990029249488801793", "video_node": "16", "video_field": "video", "size_node": "71"},
}
TERMINAL_SUCCESS = {"SUCCESS", "SUCCEEDED", "COMPLETED", "COMPLETE", "FINISHED"}
TERMINAL_FAILURE = {"FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED"}


class ProviderFailure(RuntimeError):
    pass


def provider_order(requested: str, configured: str) -> list[str]:
    if requested != "auto":
        return [requested]
    result = [item.strip().lower() for item in configured.split(",") if item.strip().lower() in PROVIDERS]
    return list(dict.fromkeys(result)) or list(PROVIDERS)


def build_payload(provider: str, source_uri: str, max_resolution: int) -> dict[str, Any]:
    spec = PROVIDERS[provider]
    size_description = "设置最短边" if provider == "seedvr2" else "最大分辨率设置"
    return {
        "nodeInfoList": [
            {"nodeId": spec["video_node"], "fieldName": spec["video_field"], "fieldValue": source_uri,
             "description": "上传视频"},
            {"nodeId": spec["size_node"], "fieldName": "value", "fieldValue": str(max_resolution),
             "description": size_description},
        ],
        "instanceType": "plus" if max_resolution > 1920 else "default",
        "usePersonalQueue": "false",
    }


def _find_result_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    if isinstance(value, dict):
        preferred = ("url", "fileUrl", "file_url", "videoUrl", "video_url")
        for key in preferred:
            found = _find_result_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _find_result_url(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_result_url(item)
            if found:
                return found
    return None


def run_provider(
    provider: str,
    request_data: dict[str, Any],
    update_progress,
) -> tuple[str, str]:
    settings = get_settings()
    token = settings.runninghub_api_token.get_secret_value() if settings.runninghub_api_token else ""
    if not token:
        raise ProviderFailure("RunningHub API token is not configured")
    base = settings.runninghub_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    spec = PROVIDERS[provider]
    timeout = httpx.Timeout(settings.video_upscale_request_timeout_seconds, connect=20)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=False) as client:
        response = client.post(
            f"{base}/openapi/v2/run/ai-app/{spec['app_id']}",
            json=build_payload(provider, request_data["source_uri"], request_data["max_resolution"]),
        )
        response.raise_for_status()
        body = response.json()
        task_id = str(body.get("taskId") or "")
        if not task_id:
            raise ProviderFailure("provider did not return taskId")
        deadline = time.monotonic() + settings.video_upscale_provider_timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(settings.video_upscale_poll_seconds)
            query = client.post(f"{base}/openapi/v2/query", json={"taskId": task_id})
            query.raise_for_status()
            payload = query.json()
            state = str(payload.get("status") or "").upper()
            if state in TERMINAL_FAILURE:
                reason = payload.get("errorMessage") or payload.get("failedReason") or state
                raise ProviderFailure(f"provider terminal failure: {str(reason)[:240]}")
            result_url = _find_result_url(payload.get("results"))
            if state in TERMINAL_SUCCESS and result_url:
                return result_url, task_id
            progress = payload.get("progress")
            update_progress(int(progress) if isinstance(progress, (int, float)) else 50)
        raise ProviderFailure("provider query timed out")


def _run_ffmpeg(arguments: list[str]) -> None:
    settings = get_settings()
    ffmpeg_bin = (
        imageio_ffmpeg.get_ffmpeg_exe()
        if settings.video_upscale_ffmpeg_bin == "auto"
        else settings.video_upscale_ffmpeg_bin
    )
    try:
        subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.video_upscale_ffmpeg_timeout_seconds,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        raise RuntimeError(f"video preparation failed: {detail[-400:]}") from exc


def split_video(source: Path, directory: Path, segment_seconds: float) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    pattern = directory / "segment-%04d.mp4"
    _run_ffmpeg([
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})",
        "-c:a", "aac", "-b:a", "192k", "-f", "segment",
        "-segment_time", str(segment_seconds), "-reset_timestamps", "1", str(pattern),
    ])
    segments = sorted(directory.glob("segment-*.mp4"))
    if not segments:
        raise RuntimeError("video splitter produced no segments")
    return segments


def _upload_file(path: Path, job_id: str, stage: str, filename: str) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    with path.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data={"external_ref": f"{job_id}:{stage}", "stage": stage, "actor": "video-upscale-worker"},
            files={"file": (filename, stream, "video/mp4")},
            timeout=httpx.Timeout(settings.video_upscale_upload_timeout_seconds, connect=30),
        )
    response.raise_for_status()
    url = str(response.json().get("uri") or "")
    if not url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return url


def _process_segment(
    index: int,
    segment_url: str,
    request_data: dict[str, Any],
    configured_order: str,
    max_attempts: int,
    output_dir: Path,
) -> dict[str, Any]:
    requested = str(request_data.get("provider", "auto"))
    providers = provider_order(requested, configured_order)
    if requested != "auto":
        providers = providers * max_attempts
    else:
        providers = (providers * max_attempts)[:max_attempts]
    attempts: list[dict[str, str]] = []
    for attempt_number, provider in enumerate(providers, start=1):
        try:
            result_url, _ = run_provider(
                provider,
                {**request_data, "source_uri": segment_url},
                lambda _progress: None,
            )
            downloaded = download_public_media(
                result_url, output_dir, f"upscaled-{index:04d}", VIDEO_MEDIA,
                get_settings().video_upscale_max_download_bytes,
                get_settings().video_upscale_provider_timeout_seconds,
            )
            return {
                "index": index, "provider": provider, "attempt_count": attempt_number,
                "attempts": attempts + [{"provider": provider, "status": "succeeded"}],
                "path": downloaded.path,
            }
        except Exception as exc:
            # 记完整信息：只留类型名时，"3 次尝试都失败"这条日志无法定位到底是
            # 供应商报错、下载失败还是网络超时。
            attempts.append({
                "provider": provider, "status": "failed", "error": type(exc).__name__,
                "detail": str(exc)[:400],
            })
    raise ProviderFailure(
        f"segment {index} failed after {len(attempts)} attempts: "
        + "; ".join(f"{item['provider']}={item['error']}({item.get('detail', '')})" for item in attempts)
    )


def _media_metadata(path: Path) -> dict[str, Any]:
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        return next(reader)
    finally:
        reader.close()


def merge_segments(
    paths: list[Path],
    original_segments: list[Path],
    source: Path,
    work_dir: Path,
    max_resolution: int,
) -> Path:
    source_meta = _media_metadata(source)
    source_width, source_height = source_meta["size"]
    scale = max_resolution / max(source_width, source_height)
    width = max(2, round(source_width * scale / 2) * 2)
    height = max(2, round(source_height * scale / 2) * 2)
    fps = float(source_meta.get("fps") or 30)
    normalized_dir = work_dir / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for index, (path, original) in enumerate(zip(paths, original_segments, strict=True), start=1):
        target_duration = imageio_ffmpeg.count_frames_and_secs(str(original))[1]
        target = normalized_dir / f"segment-{index:04d}.mp4"
        _run_ffmpeg([
            "-i", str(path), "-an",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={target_duration}",
            "-t", str(target_duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ])
        normalized.append(target)
    concat_file = work_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{path.as_posix()}'\n" for path in normalized), encoding="utf-8")
    video_only = work_dir / "merged-video.mp4"
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(video_only)])
    final = work_dir / "upscaled.mp4"
    _run_ffmpeg([
        "-i", str(video_only), "-i", str(source), "-map", "0:v:0", "-map", "1:a?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-movflags", "+faststart", str(final),
    ])
    return final


@celery_app.task(bind=True, name="control_plane.video_upscale", time_limit=14400, soft_time_limit=14340)
def upscale_video(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    started = time.perf_counter()
    work_dir = settings.video_upscale_work_dir / job_id
    input_dir, segment_dir, output_dir = work_dir / "input", work_dir / "segments", work_dir / "outputs"
    input_dir.mkdir(parents=True, exist_ok=True); output_dir.mkdir(parents=True, exist_ok=True)
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 2})
        source = download_public_media(
            request_data["source_uri"], input_dir, "source", VIDEO_MEDIA,
            settings.video_upscale_max_download_bytes, settings.video_upscale_provider_timeout_seconds,
        ).path
        self.update_state(state="PROGRESS", meta={"stage": "splitting", "progress": 6})
        segments = split_video(source, segment_dir, settings.video_upscale_segment_seconds)
        segment_urls = [
            _upload_file(path, job_id, "media.video_upscale.segment_input", f"segment-{index:04d}.mp4")
            for index, path in enumerate(segments, start=1)
        ]
        self.update_state(state="PROGRESS", meta={
            "stage": "upscaling_segments", "progress": 12, "segment_count": len(segments), "completed_segments": 0,
        })
        results: dict[int, dict[str, Any]] = {}
        concurrency = max(1, min(settings.video_upscale_segment_concurrency, len(segments)))
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    _process_segment, index, url, request_data,
                    settings.video_upscale_auto_provider_order,
                    settings.video_upscale_segment_attempts, output_dir,
                ): index
                for index, url in enumerate(segment_urls, start=1)
            }
            for future in as_completed(futures):
                result = future.result()
                results[result["index"]] = result
                completed = len(results)
                self.update_state(state="PROGRESS", meta={
                    "stage": "upscaling_segments", "progress": 12 + int(73 * completed / len(segments)),
                    "segment_count": len(segments), "completed_segments": completed,
                })
        ordered = [results[index] for index in range(1, len(segments) + 1)]
        self.update_state(state="PROGRESS", meta={"stage": "merging", "progress": 88})
        final = merge_segments(
            [item["path"] for item in ordered], segments, source, work_dir,
            int(request_data["max_resolution"]),
        )
        self.update_state(state="PROGRESS", meta={"stage": "uploading", "progress": 95})
        result_url = _upload_file(final, job_id, "media.video_upscale", "upscaled.mp4")
        return {
            "job_id": job_id, "status": "succeeded", "requested_provider": request_data.get("provider", "auto"),
            "provider": "mixed" if len({item["provider"] for item in ordered}) > 1 else ordered[0]["provider"],
            "fallback_used": any(item["attempt_count"] > 1 for item in ordered),
            "segment_count": len(ordered), "segment_seconds_limit": 12,
            "segments": [{key: value for key, value in item.items() if key != "path"} for item in ordered],
            "result_url": result_url, "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
