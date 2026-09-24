"""mxapi(Suno) 的音乐 / 音效生成。

上游契约有两个和中台既有渠道都不一样的地方，都在这里处理掉了：

1. **一次提交出两个 task、两首歌。** 提交返回 `data.task_ids` 是两条，不是一个；
   而每条 task 的查询响应里 `result.fileInfo` 只代表自己那一首，但 `result.extend`
   会把**两首**都列出来（两个响应里都是两首，不是各列各的）。所以：
   认领要靠 `result.custom_id` 去 extend 里对 id，按顺序取会拿到别人的歌。
2. **成品是 opus-in-mp4 的 .m4a，且 http。** 中台的下载器只收 https（`mp3Url` 是 http，
   被 `media_fetch` 的校验拒掉），而 https 的那份是 `extend[i].media_urls[0].url`
   （CloudFront，`content_type: m4a-opus`）。下载用 ASR_MEDIA（它的签名嗅探认 ftyp isom），
   再转成 mp3 后回传内核——顺带解决「opus 装在 mp4 容器里，剪辑软件不认」这件事。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
import imageio_ffmpeg

from .celery_app import celery_app
from .concurrency import job_slot
from .config import get_settings
from .generation_tasks import (
    FAILURE_STATES,
    RETRYABLE,
    SUCCESS_STATES,
    _cancel_requested,
    _compatible,
    _error_detail,
    _headers,
    _response_body,
    _store,
)
from .media_fetch import ASR_MEDIA, download_public_media

SOUND_MODEL = "suno-sound"
MUSIC_MODEL = "suno-v6"
POLL_SECONDS = 5
# 文档没写数字状态码的含义，这是 Suno 一贯的约定（3 = 完成），
# 与实测吻合：完成的响应里 `data.result.status` 就是 '3'，`data.status` 是 'completed'。
NUMERIC_STATES = {"0": "queued", "1": "queued", "2": "processing", "3": "completed", "4": "failed"}
NULLISH = {"", "none", "null", "false"}


def _accepted(body: dict[str, Any]) -> bool:
    """mxapi 的应用层错误码也走 HTTP 200，而且 code 是**字符串**（实测 '200' / '400'）。"""
    code = body.get("code")
    if code is None:
        return body.get("ok") is not False
    try:
        return int(code) == 200
    except (TypeError, ValueError):
        return False


def _task_ids(body: dict[str, Any]) -> list[str]:
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    ids = data.get("task_ids") or data.get("task_id")
    if isinstance(ids, str):
        return [ids] if ids.strip() else []
    if isinstance(ids, list):
        return [str(item) for item in ids if item]
    return []


def _extend_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    """`result.extend` 是**字符串形式的 JSON 数组**，每一项是一首歌。"""
    value = result.get("extend")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _own_entry(result: dict[str, Any]) -> dict[str, Any] | None:
    """认领这个 task 自己的那一首：`custom_id` 就是它的 clip id。

    兜底：custom_id 缺失时从 `fileInfo.mp3Url` 的文件名反推（同名规则实测成立）。
    都对不上就返回 None——宁可少收一首，也不要把同一个 job 里另一首记成自己的。
    """
    info = result.get("fileInfo")
    info = info if isinstance(info, dict) else {}
    ident = str(result.get("custom_id") or "").strip()
    if not ident:
        ident = Path(unquote(urlsplit(str(info.get("mp3Url") or "")).path)).stem
    for entry in _extend_entries(result):
        if ident and str(entry.get("id") or "") == ident:
            return entry
    return None


def _https(value: Any) -> str:
    url = str(value or "")
    return url if url.startswith("https://") else ""


def _song_url(entry: dict[str, Any], result: dict[str, Any]) -> str:
    """优先 https 直链；都没有才退回 `proxy_url`（它是把那条 http 直链包了一层的 https 代理）。"""
    for item in entry.get("media_urls") or []:
        if isinstance(item, dict):
            url = _https(item.get("url"))
            if url:
                return url
    return _https(result.get("proxy_url"))


def _duration(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _task_state(body: dict[str, Any]) -> str:
    """查任务返回体里的状态。

    注意 mxapi 把「没有错误」写成字符串 `"None"`，所以不能拿真值判断 error——
    否则每条正常响应都会被当成失败。
    """
    data = body.get("data")
    if not isinstance(data, dict):
        return ""
    if str(data.get("error") or "").strip().lower() not in NULLISH:
        return "failed"
    result = data.get("result")
    if isinstance(result, dict):
        numeric = str(result.get("status") or "").strip()
        if numeric in NUMERIC_STATES:
            return NUMERIC_STATES[numeric]
        if str(result.get("errormsg") or "").strip().lower() not in NULLISH:
            return "failed"
    return str(data.get("status") or "").strip().lower()


def _song(body: dict[str, Any]) -> dict[str, Any] | None:
    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    result = data.get("result")
    result = result if isinstance(result, dict) else {}
    info = result.get("fileInfo")
    info = info if isinstance(info, dict) else {}
    entry = _own_entry(result)
    if entry is None:
        return None
    url = _song_url(entry, result)
    if not url:
        return None
    metadata = entry.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "clip_id": str(result.get("custom_id") or entry.get("id") or ""),
        "url": url,
        "title": str(entry.get("title") or ""),
        "duration_seconds": _duration(info.get("duration")),
        "cover_url": _https(entry.get("image_url")) or _https(info.get("cosUrl")),
        "model_name": str(entry.get("model_name") or ""),
        "tags": str(metadata.get("tags") or ""),
        # 上游把歌词（或音效的描述）放在 metadata.prompt 里回传。
        "lyrics": str(metadata.get("prompt") or ""),
    }


def _payload(binding: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """灵感模式给 `gpt_description_prompt`，自定义模式给 `prompt`(歌词) + `tags`。

    两种模式是**互斥**的（api 层已经拦过），音效走另一个端点、字段也不同。
    """
    if request["model"] == SOUND_MODEL:
        return {
            "mv": request["sound_model"],
            "title": request["title"],
            "tags": request["tags"],
            "loop": bool(request.get("loop")),
        }
    body: dict[str, Any] = {
        "mv": request["music_model"],
        "make_instrumental": bool(request.get("instrumental")),
    }
    if request.get("title"):
        body["title"] = request["title"]
    if request.get("lyrics"):
        body["prompt"] = request["lyrics"]
        if request.get("tags"):
            body["tags"] = request["tags"]
    else:
        body["gpt_description_prompt"] = request["prompt"]
    metadata: dict[str, Any] = {}
    if request.get("vocal_gender"):
        metadata["vocal_gender"] = request["vocal_gender"]
    sliders = {
        key: request[key]
        for key in ("style_weight", "weirdness_constraint")
        if request.get(key) is not None
    }
    if sliders:
        metadata["control_sliders"] = sliders
    if metadata:
        body["metadata"] = metadata
    return body


def _to_mp3(path: Path) -> Path:
    """成品是 opus-in-mp4（ftyp isom），转成 mp3 顺带让文件名与内容一致。"""
    executable = shutil.which("ffmpeg")
    if not executable:
        try:
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise RuntimeError("ffmpeg is required to convert the result to mp3") from exc
    output = path.with_suffix(".mp3")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(output),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            timeout=get_settings().audio_generation_transcode_timeout_seconds,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("unable to convert the result to mp3") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("mp3 conversion produced an empty file")
    return output


def _persist_audio_results(
    songs: list[dict[str, Any]], request: dict[str, Any], job_id: str
) -> list[str]:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        return [song["url"] for song in songs]
    directory = settings.audio_generation_work_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    persisted: list[str] = []
    try:
        for index, song in enumerate(songs, start=1):
            downloaded = download_public_media(
                song["url"],
                directory,
                f"result-{index}",
                ASR_MEDIA,
                settings.audio_generation_result_max_bytes,
                settings.audio_generation_download_timeout_seconds,
            )
            upload_path = _to_mp3(downloaded.path)
            data = {
                "external_ref": request.get("external_ref") or job_id,
                "run_id": "",
                "campaign_id": "",
                "project_id": "",
                "stage": f"media.audio_generation.result_{index}",
                "actor": "audio-generation-worker",
            }
            with upload_path.open("rb") as stream:
                response = httpx.post(
                    settings.kernel_upload_url,
                    headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
                    data=data,
                    files={"file": (upload_path.name, stream, "audio/mpeg")},
                    timeout=httpx.Timeout(
                        settings.audio_generation_upload_timeout_seconds,
                        connect=30,
                    ),
                )
            response.raise_for_status()
            result_url = str(response.json().get("uri") or "")
            if not result_url.startswith("https://"):
                raise RuntimeError("kernel upload did not return an HTTPS URL")
            persisted.append(result_url)
        return persisted
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _cancelled(store, job_id: str, started: float) -> dict[str, Any]:
    store.update_job(
        job_id,
        status="cancelled",
        stage="cancelled",
        finished_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return store.job(job_id)


def _poll_songs(
    store,
    job_id: str,
    request: dict[str, Any],
    binding: dict[str, Any],
    task_ids: list[str],
    started: float,
) -> dict[str, Any]:
    """两条 task 各自成歌，谁先好谁先入库；一条超时不影响另一条已经拿到的成品。"""
    deadline = time.monotonic() + binding["timeout_seconds"]
    pending = list(task_ids)
    songs: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    while pending and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        if _cancel_requested(store, job_id):
            return _cancelled(store, job_id, started)
        for task_id in list(pending):
            path = binding["query_path"].replace("{task_id}", task_id)
            try:
                with httpx.Client(timeout=30, follow_redirects=False) as client:
                    response = client.get(
                        urljoin(binding["base_url"].rstrip("/") + "/", path.lstrip("/")),
                        headers=_headers(binding),
                    )
                if response.status_code in RETRYABLE:
                    continue
                response.raise_for_status()
                body = _response_body(response)
            except (httpx.TimeoutException, httpx.NetworkError):
                continue
            state = _task_state(body)
            if state in SUCCESS_STATES:
                song = _song(body)
                pending.remove(task_id)
                if song:
                    songs[task_id] = song
                else:
                    failures.append(f"{task_id}: completed without an audio result")
            elif state in FAILURE_STATES:
                pending.remove(task_id)
                failures.append(
                    f"{task_id}: {_error_detail(body, 'upstream generation failed')[:160]}"
                )
        store.update_job(
            job_id,
            upstream_response_json=json.dumps(
                {"task_ids": task_ids, "completed": len(songs), "pending": len(pending)}
            ),
        )
    for task_id in pending:
        failures.append(f"{task_id}: upstream generation timed out")
    ordered = [songs[task_id] for task_id in task_ids if task_id in songs]
    if not ordered:
        store.update_job(
            job_id,
            status="failed",
            stage="failed",
            upstream_response_json=json.dumps({"task_ids": task_ids, "task_errors": failures}),
            error="; ".join(failures) or "upstream generation timed out",
            finished_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return store.job(job_id)
    if _cancel_requested(store, job_id):
        return _cancelled(store, job_id, started)
    store.update_job(job_id, stage="uploading_oss")
    urls = _persist_audio_results(ordered, request, job_id)
    store.update_job(
        job_id,
        status="succeeded",
        stage="completed",
        result_urls_json=json.dumps(urls),
        upstream_response_json=json.dumps(
            {
                "task_ids": task_ids,
                "songs": [
                    {**song, "url": url} for song, url in zip(ordered, urls, strict=False)
                ],
                "task_errors": failures,
            }
        ),
        error=None,
        finished_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return store.job(job_id)


@celery_app.task(
    bind=True, name="control_plane.audio_generation", time_limit=3600, soft_time_limit=3540
)
def generate_audio(self, job_id: str) -> dict[str, Any]:
    # 排队状态写在自己的 store 里（不是 Celery state），和视频生成同一套。
    with job_slot(
        "audio_generation", on_wait=lambda limit: _store().update_job(job_id, stage="waiting_slot")
    ):
        return _generate_audio(job_id)


def _generate_audio(job_id: str) -> dict[str, Any]:
    store = _store()
    job = store.job(job_id, include_request=True)
    request = job["request"]
    started = time.monotonic()
    requested = request.get("channel") or "mxapi"
    order = [requested] if requested != "auto" else ["mxapi"]
    store.update_job(
        job_id,
        status="running",
        stage="selecting_channel",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    failures: list[str] = []
    for index, code in enumerate(order):
        try:
            binding = store.binding(request["model"], code)
        except Exception:
            continue
        if (
            not binding["enabled"]
            or not binding["channel_enabled"]
            or binding["health_status"] != "online"
            or not _compatible(binding, request)
        ):
            continue
        try:
            store.update_job(
                job_id, channel_id=binding["channel_id"], stage="submitting", fallback_count=index
            )
            with httpx.Client(
                timeout=httpx.Timeout(binding["timeout_seconds"], connect=20),
                follow_redirects=False,
            ) as client:
                response = client.post(
                    urljoin(binding["base_url"].rstrip("/") + "/", binding["submit_path"].lstrip("/")),
                    headers=_headers(binding),
                    json=_payload(binding, request),
                )
            if response.status_code >= 400:
                try:
                    error_body = _response_body(response)
                except ValueError:
                    error_body = {}
                raise RuntimeError(
                    f"upstream HTTP {response.status_code}: "
                    f"{_error_detail(error_body, 'request rejected')[:160]}"
                )
            body = _response_body(response)
            if not _accepted(body):
                raise RuntimeError(
                    f"upstream rejected request: "
                    f"{_error_detail(body, 'request rejected')[:160]}"
                )
            task_ids = _task_ids(body)
            if not task_ids:
                raise RuntimeError(
                    f"upstream rejected request: "
                    f"{_error_detail(body, 'upstream response did not contain task ids')[:120]}"
                )
            store.update_job(
                job_id,
                upstream_task_id=",".join(task_ids),
                stage="polling",
                upstream_response_json=json.dumps(
                    {"accepted": True, "status_code": response.status_code, "task_ids": task_ids}
                ),
            )
            return _poll_songs(store, job_id, request, binding, task_ids, started)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            failures.append(f"{code}:{type(exc).__name__}")
            break
        except Exception as exc:
            failures.append(f"{code}:{str(exc)[:200]}")
            break
    store.update_job(
        job_id,
        status="failed",
        stage="failed",
        error="; ".join(failures) or "no enabled compatible channel",
        finished_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return store.job(job_id)
