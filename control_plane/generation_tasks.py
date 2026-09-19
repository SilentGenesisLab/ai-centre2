from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import imageio_ffmpeg

from .ai_capabilities import CapabilityStore
from .celery_app import celery_app
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media

RETRYABLE={408,429,500,502,503,504}
SUCCESS_STATES = {"completed", "succeeded", "success"}
FAILURE_STATES = {"fail", "failed", "error", "cancelled", "canceled"}
PIXEL_ASPECTS={
    "1K":{"1:1":"1024x1024","2:3":"832x1248","3:2":"1248x832","3:4":"768x1024","4:3":"1024x768","9:16":"768x1344","16:9":"1344x768"},
    "2K":{"1:1":"2048x2048","2:3":"1664x2496","3:2":"2496x1664","3:4":"1536x2048","4:3":"2048x1536","9:16":"1152x2048","16:9":"2048x1152"},
    "4K":{"1:1":"4096x4096","2:3":"3328x4992","3:2":"4992x3328","3:4":"3072x4096","4:3":"4096x3072","9:16":"2304x4096","16:9":"4096x2304"},
}


class UpstreamTerminalError(RuntimeError):
    """A confirmed upstream failure for which auto-routing may safely retry."""

def _store():
    s=get_settings(); return CapabilityStore(s.ai_capabilities_db_path,s.service_token)
def _headers(binding:dict[str,Any])->dict[str,str]:
    key=binding.get("credential",""); kind=binding.get("auth_type","none")
    if not key:return {}
    if kind=="x-api-key":return {"X-API-Key":key}
    if kind=="bearer":return {"Authorization":f"Bearer {key}"}
    return {}
def _payload(
    binding: dict[str, Any],
    request: dict[str, Any],
    blank_image_url: str | None = None,
) -> dict[str, Any]:
    images = list(request.get("reference_image_urls", []))
    videos = list(request.get("reference_video_urls", []))
    audios = list(request.get("reference_audio_urls", []))
    if not images and not videos and not audios and blank_image_url:
        images = [blank_image_url]
    if binding["adapter"]=="jmapi":
        return {"image_urls":images,"audio_urls":audios,"video_urls":videos,"prompt":request["prompt"],"model_version":binding["upstream_model"],"duration":request["duration_seconds"],"ratio":request["aspect_ratio"],"video_resolution":request["resolution"],"poll":None}
    if binding["adapter"]=="libtv":
        return {"model":binding["upstream_model"],"prompt":request["prompt"],"params":{"modeType":"mixed2video","duration":request["duration_seconds"],"ratio":request["aspect_ratio"],"resolution":request["resolution"],"enableSound":"on" if request.get("sound") else "off"},"imageUrls":images,"videoUrls":videos,"audioUrls":audios}
    if binding["adapter"]=="grsai":
        model=binding["upstream_model"]
        aspect=request["aspect_ratio"]
        if model in {"gpt-image-2.5-sunburst","gpt-image-2.5-flare"}:
            aspect=PIXEL_ASPECTS[request.get("image_size","1K")][aspect]
        payload={"model":model,"prompt":request["prompt"],"aspectRatio":aspect,"urls":request.get("reference_image_urls",[]),"shutProgress":True}
        if binding["upstream_model"]=="nano-banana-2": payload["imageSize"]=request.get("image_size","1K")
        return payload
    raise ValueError("unsupported generation channel adapter")
def _unwrap(body:dict[str,Any])->dict[str,Any]:
    stdout=body.get("stdout")
    if isinstance(stdout,str):
        try:
            value=json.loads(stdout)
            if isinstance(value,dict): return value
        except json.JSONDecodeError: pass
    return body
def _response_body(response:httpx.Response)->dict[str,Any]:
    try:
        value=response.json()
        if isinstance(value,dict): return value
    except (ValueError,json.JSONDecodeError): pass
    for line in reversed(response.text.splitlines()):
        text=line.strip()
        if text.startswith("data:"):
            try:
                value=json.loads(text[5:].strip())
                if isinstance(value,dict): return value
            except json.JSONDecodeError: continue
    raise ValueError("upstream response did not contain JSON")
def _task_id(body:dict[str,Any])->str|None:
    body=_unwrap(body)
    for key in ("submit_id","taskId","task_id","id"):
        if body.get(key):return str(body[key])
    for key in ("task", "data", "result"):
        data=body.get(key)
        if isinstance(data,dict) and data:
            task_id = _task_id(data)
            if task_id:
                return task_id
    return None


def _urls_from_container(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            if isinstance(item, str):
                output.extend(_urls_from_container(item))
            elif isinstance(item, dict):
                for key in ("url", "video_url", "image_url"):
                    if item.get(key):
                        output.extend(_urls_from_container(item[key]))
                        break
        return output
    if isinstance(value, dict):
        for key in ("urls", "videos", "images", "image_urls", "video_urls", "results", "url", "video_url", "image_url"):
            if value.get(key):
                return _urls_from_container(value[key])
    return []


def _result(body:dict[str,Any])->list[str]:
    body=_unwrap(body)
    candidates: list[Any] = []
    if isinstance(body.get("result_json"), dict):
        candidates.append(body["result_json"])
    task = body.get("task")
    if isinstance(task, dict):
        candidates.append(task.get("result"))
    candidates.extend((body.get("result"), body.get("data"), body))
    for candidate in candidates:
        urls = _urls_from_container(candidate)
        if urls:
            return list(dict.fromkeys(urls))
    return []


def _state(body: dict[str, Any]) -> str:
    normalized = _unwrap(body)
    candidates = [normalized]
    for key in ("task", "data", "result"):
        if isinstance(normalized.get(key), dict):
            candidates.append(normalized[key])
    for candidate in candidates:
        for key in ("gen_status", "status", "state"):
            if candidate.get(key) is not None:
                return str(candidate[key]).lower()
    return ""


def _error_detail(body: dict[str, Any], default: str) -> str:
    candidates = [body, _unwrap(body)]
    normalized = candidates[-1]
    if isinstance(normalized.get("task"), dict):
        candidates.append(normalized["task"])
    for candidate in candidates:
        for key in (
            "stderr",
            "error",
            "fail_reason",
            "failure_reason",
            "detail",
            "msg",
            "message",
        ):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return default


def _accepted(body: dict[str, Any]) -> bool:
    if body.get("ok") is False:
        return False
    if "exit_code" in body:
        try:
            return int(body["exit_code"]) == 0
        except (TypeError, ValueError):
            return False
    return True
def _compatible(binding:dict[str,Any],r:dict[str,Any])->bool:
    c=binding["capabilities"]
    media_supported = (
        len(r.get("reference_image_urls", [])) <= c.get("images", 0)
        and len(r.get("reference_video_urls", [])) <= c.get("videos", 0)
        and len(r.get("reference_audio_urls", [])) <= c.get("audios", 0)
    )
    if not media_supported:
        return False
    # jmapi's Seedance 2.0 VIP contract rejects 480p and requires its
    # Seedance 2.5 model for that resolution. `auto` can select libtv instead.
    if (
        binding.get("adapter") == "jmapi"
        and binding.get("upstream_model") == "seedance2.0_vip"
        and str(r.get("resolution") or "").lower() == "480p"
    ):
        return False
    return True
def probe_channel(channel:dict[str,Any])->tuple[bool,Any,str|None]:
    if not channel.get("base_url"): return False,None,"Base URL未配置"
    path={"jmapi":"/jmapi/status","libtv":"/libtv/api/v1/video/balances","grsai":"/v1/draw/result","local_h3":"/health"}.get(channel["adapter"],"/health")
    try:
        with httpx.Client(timeout=min(channel["timeout_seconds"],20),follow_redirects=False) as client:
            if channel["adapter"]=="grsai": response=client.post(urljoin(channel["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(channel),json={"id":"connection-test"})
            else: response=client.get(urljoin(channel["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(channel))
        if response.status_code>=400:return False,None,f"HTTP {response.status_code}"
        return True,response.json(),None
    except Exception as exc:return False,None,type(exc).__name__


def _persist_video_results(
    urls: list[str], request: dict[str, Any], job_id: str
) -> list[str]:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        return urls
    directory = settings.video_generation_work_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    persisted: list[str] = []
    try:
        for index, url in enumerate(urls, start=1):
            downloaded = download_public_media(
                url,
                directory,
                f"result-{index}",
                VIDEO_MEDIA,
                settings.video_generation_result_max_bytes,
                settings.video_generation_download_timeout_seconds,
            )
            upload_path = _prepare_video_for_upload(downloaded.path, request)
            data = {
                "external_ref": request.get("external_ref") or job_id,
                "run_id": "",
                "campaign_id": "",
                "project_id": "",
                "stage": f"media.video_generation.result_{index}",
                "actor": "video-generation-worker",
            }
            with upload_path.open("rb") as stream:
                response = httpx.post(
                    settings.kernel_upload_url,
                    headers={
                        "Authorization": f"Bearer {settings.kernel_api_token}"
                    },
                    data=data,
                    files={
                        "file": (
                            upload_path.name,
                            stream,
                            downloaded.content_type or "video/mp4",
                        )
                    },
                    timeout=httpx.Timeout(
                        settings.video_generation_upload_timeout_seconds,
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


def _prepare_video_for_upload(path: Path, request: dict[str, Any]) -> Path:
    """Honor sound=false even when an upstream returns an unexpected audio track."""
    if request.get("sound", False):
        return path
    executable = shutil.which("ffmpeg")
    if not executable:
        try:
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise RuntimeError(
                "ffmpeg is required to remove an unexpected audio track"
            ) from exc
    output = path.with_name(f"{path.stem}-silent{path.suffix}")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
    ]
    if path.suffix.lower() in {".mp4", ".mov"}:
        command.extend(("-movflags", "+faststart"))
    command.append(str(output))
    try:
        subprocess.run(command, check=True, timeout=900, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("unable to remove unexpected result audio track") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("audio removal produced an empty video")
    return output


def _cancel_requested(store: CapabilityStore, job_id: str) -> bool:
    return store.job(job_id).get("status") in {"cancel_requested", "cancelled"}


def _finish_success(
    store: CapabilityStore,
    job_id: str,
    request: dict[str, Any],
    task_id: str,
    state: str,
    urls: list[str],
    started: float,
) -> dict[str, Any]:
    if _cancel_requested(store, job_id):
        store.update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            finished_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return store.job(job_id)
    if request.get("model") == "seedance-2.0":
        store.update_job(job_id, stage="uploading_oss")
        urls = _persist_video_results(urls, request, job_id)
    elapsed = round(time.monotonic() - started, 3)
    store.update_job(
        job_id,
        upstream_task_id=task_id,
        status="succeeded",
        stage="completed",
        result_urls_json=json.dumps(urls),
        upstream_response_json=json.dumps(
            {"status": state or "completed", "result_count": len(urls)}
        ),
        finished_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=elapsed,
    )
    return store.job(job_id)

@celery_app.task(bind=True,name="control_plane.video_generation",time_limit=14400,soft_time_limit=14340)
def generate(self,job_id:str)->dict[str,Any]:
    settings = get_settings()
    store=_store(); job=store.job(job_id,include_request=True); req=job["request"]; started=time.monotonic()
    requested=req.get("channel") or "jmapi"; order=[requested] if requested!="auto" else ["jmapi","libtv"]
    store.update_job(job_id,status="running",stage="selecting_channel",started_at=datetime.now(timezone.utc).isoformat())
    failures=[]
    for index,code in enumerate(order):
        try: binding=store.binding(req["model"],code)
        except Exception: continue
        if not binding["enabled"] or not binding["channel_enabled"] or binding["health_status"]!="online" or not _compatible(binding,req): continue
        submitted = False
        try:
            store.update_job(job_id,channel_id=binding["channel_id"],stage="submitting",fallback_count=index)
            timeout=httpx.Timeout(binding["timeout_seconds"],connect=20)
            with httpx.Client(timeout=timeout,follow_redirects=False) as client:
                response=client.post(urljoin(binding["base_url"].rstrip("/")+"/",binding["submit_path"].lstrip("/")),headers=_headers(binding),json=_payload(binding,req,settings.video_generation_blank_image_url))
            if response.status_code>=400:
                try:
                    error_body = _response_body(response)
                except ValueError:
                    error_body = {}
                raise RuntimeError(
                    f"upstream HTTP {response.status_code}: "
                    f"{_error_detail(error_body, 'request rejected')[:160]}"
                )
            body=_response_body(response); tid=_task_id(body); inline_urls=_result(body)
            if not _accepted(body):
                raise RuntimeError(
                    f"upstream rejected request: "
                    f"{_error_detail(body, 'request rejected')[:160]}"
                )
            if not tid:
                message=_error_detail(body,"upstream response did not contain task id")
                raise RuntimeError(f"upstream rejected request: {message[:120]}")
            submitted = True
            inline_state=_state(body)
            if inline_state in FAILURE_STATES:
                raise UpstreamTerminalError(
                    f"upstream generation failed: "
                    f"{_error_detail(body, 'upstream generation failed')[:160]}"
                )
            if inline_urls and inline_state in SUCCESS_STATES:
                return _finish_success(store,job_id,req,tid,inline_state,inline_urls,started)
            store.update_job(job_id,upstream_task_id=tid,stage="polling",upstream_response_json=json.dumps({"accepted":True,"status_code":response.status_code}))
            deadline=time.monotonic()+binding["timeout_seconds"]
            missing_result_polls = 0
            while time.monotonic()<deadline:
                time.sleep(5)
                if _cancel_requested(store, job_id):
                    store.update_job(
                        job_id,
                        status="cancelled",
                        stage="cancelled",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        elapsed_seconds=round(time.monotonic()-started,3),
                    )
                    return store.job(job_id)
                path=binding["query_path"].replace("{task_id}",tid)
                try:
                    with httpx.Client(timeout=30,follow_redirects=False) as client:
                        if binding["adapter"]=="jmapi": q=client.post(urljoin(binding["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(binding),json={"submit_id":tid})
                        elif binding["adapter"]=="grsai": q=client.post(urljoin(binding["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(binding),json={"id":tid})
                        else:q=client.get(urljoin(binding["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(binding))
                    if q.status_code in RETRYABLE:
                        continue
                    q.raise_for_status()
                    status=_response_body(q)
                except (httpx.TimeoutException,httpx.NetworkError):
                    continue
                state=_state(status); urls=_result(status)
                if state in SUCCESS_STATES:
                    if urls:
                        return _finish_success(store,job_id,req,tid,state,urls,started)
                    missing_result_polls += 1
                    if missing_result_polls >= 3:
                        raise UpstreamTerminalError(
                            "upstream completed without a result URL"
                        )
                    continue
                if state in FAILURE_STATES:
                    detail=_error_detail(status,"upstream generation failed")
                    raise UpstreamTerminalError(
                        f"upstream generation failed: {detail[:160]}"
                    )
            raise TimeoutError("upstream generation timed out")
        except (httpx.TimeoutException,httpx.NetworkError) as exc:
            if requested=="auto" and not submitted:failures.append(f"{code}:{type(exc).__name__}");continue
            failures.append(type(exc).__name__);break
        except UpstreamTerminalError as exc:
            failures.append(f"{code}:{str(exc)[:200]}")
            if requested == "auto":
                continue
            break
        except Exception as exc:
            failures.append(f"{code}:{str(exc)[:200]}")
            if requested=="auto" and not submitted:
                continue
            break
    elapsed=round(time.monotonic()-started,3); store.update_job(job_id,status="failed",stage="failed",error="; ".join(failures) or "no enabled compatible channel",finished_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=elapsed)
    return store.job(job_id)
