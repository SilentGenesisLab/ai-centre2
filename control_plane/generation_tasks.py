from __future__ import annotations

import base64
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
from .concurrency import job_slot
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media

RETRYABLE={408,429,500,502,503,504}
SUCCESS_STATES = {"completed", "succeeded", "success"}
FAILURE_STATES = {"fail", "failed", "error", "cancelled", "canceled"}
# 结果先转存到 AI Centre 的 OSS 再回给调用方的模型。两个理由，任一个成立就该在这里：
#  1. 上游给的是**临时**结果地址，直接回给调用方会过期；
#  2. sound=false 要求确定性摘掉音轨 —— 那一步只在转存时做（_prepare_video_for_upload），
#     而 RunningHub 的 H3 没有「静音生成」这个参数（audioMode 里没有静音档），
#     所以它的 sound=false 只能靠转存时摘。
# kernel_upload_url 没配置时 _persist_video_results 直接原样返回，所以这里是低风险的。
OSS_PERSISTED_MODELS = {"seedance-2.0", "seedance-2.5", "minimax-h3-rh-enhanced"}
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
    if not key and binding.get("adapter")=="runninghub":
        # RunningHub 的 token 是部署级环境变量（超分子系统在用同一个），**不落能力库**：
        # 复制第二份等于两处轮换、迟早对不上。渠道自己填了凭据就以渠道为准。
        settings=get_settings()
        key=settings.runninghub_api_token.get_secret_value() if settings.runninghub_api_token else ""
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
    if binding["adapter"]=="teamorouter":
        # OpenAI 兼容的生图请求体。尺寸必须由我们**算准**：这个端点对 size 不做校验，
        # 传 999x999 它照样回 200（实测会静默出一张默认尺寸的图），所以映射错了不会报错，
        # 只会安静地给错尺寸。PIXEL_ASPECTS 就是 grsai 那三个模型在用的同一张表，直接复用。
        size=PIXEL_ASPECTS.get(request.get("image_size","1K"),PIXEL_ASPECTS["1K"])
        return {"model":binding["upstream_model"],"prompt":request["prompt"],"n":1,
                "size":size.get(request.get("aspect_ratio","1:1"),size["1:1"])}
    if binding["adapter"]=="runninghub":
        # RunningHub 的参考素材是**逐张编号的平铺字段**（refImage1..9、refVideo1..3、
        # refAudio1..3），不是数组，所以只能这样按序铺开；上游没给的槽位不写，
        # 不主动填 null（占位 null 是文档 curl 的写法，实测不写也一样）。
        payload={"prompt":request["prompt"],"resolution":request["resolution"],
                 "duration":request["duration_seconds"],"aspectRatio":request["aspect_ratio"],
                 # audioMode 我们不开放：上游另有 lock_source / remix_source / reference_only
                 # 三个值，语义没有实测过，猜错是静默的（出来的是别人要的声音）。固定 native
                 # 让模型自己生成音轨；调用方的 sound=false 仍被尊重 —— 它由 OSS 转存那一步
                 # 确定性摘掉音轨（这就是 sound 在本 API 里的定义），不是靠上游静音。
                 "audioMode":"native"}
        for index,url in enumerate(images[:9],start=1): payload[f"refImage{index}"]=url
        for index,url in enumerate(videos[:3],start=1): payload[f"refVideo{index}"]=url
        for index,url in enumerate(audios[:3],start=1): payload[f"refAudio{index}"]=url
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
                for key in ("url", "video_url", "image_url", "fileUrl", "file_url", "videoUrl"):
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
            # OpenAI 风格的错误体把话埋在 `error` 这个**对象**里：
            # {"error":{"message":"...","type":"invalid_request_error","code":400}}
            # （TeamORouter 的生图与 /v1/systemone 都是这个形状）。不往下钻的话
            # 每条上游报错都只剩 "request rejected"，白丢最要紧的那句话。
            if isinstance(value, dict):
                for nested in ("message", "msg", "detail"):
                    inner = value.get(nested)
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip()
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
    # 各绑定的时长上限不同（Seedance 2.0 是 15s，2.5 是 30s），超出即换渠道而不是让上游拒。
    duration_max = c.get("duration_max")
    if duration_max and int(r.get("duration_seconds") or 0) > int(duration_max):
        return False
    # 下界同理：RunningHub 的 H3 只收 4～15 秒，2～3 秒在提交前就该被拦掉。
    duration_min = c.get("duration_min")
    if duration_min and int(r.get("duration_seconds") or 0) < int(duration_min):
        return False
    # 分辨率白名单只在绑定显式声明 resolutions 时才校验 —— Seedance 各档的上游白名单
    # 我们没有完整实测过，不替它们断言。但 768p 是本次为新模型新开的取值，必须挡住它
    # 从新入口漏到老渠道：没声明白名单的绑定一律不接 768p。
    resolution = str(r.get("resolution") or "").lower()
    declared = c.get("resolutions")
    if declared is not None:
        if resolution not in declared:
            return False
    elif resolution == "768p":
        return False
    # jmapi's Seedance 2.0 VIP contract rejects 480p and requires its
    # Seedance 2.5 model for that resolution. `auto` can select libtv instead.
    if (
        binding.get("adapter") == "jmapi"
        and binding.get("upstream_model") == "seedance2.0_vip"
        and str(r.get("resolution") or "").lower() == "480p"
    ):
        return False
    # jmapi's Seedance 2.5 only serves 480p/720p ("video_resolution must be one
    # of 480p, 720p for model_version seedance2.5"); 1080p has to go to libtv.
    if (
        binding.get("adapter") == "jmapi"
        and binding.get("upstream_model") == "seedance2.5"
        and str(r.get("resolution") or "").lower() == "1080p"
    ):
        return False
    return True
def probe_channel(channel:dict[str,Any])->tuple[bool,Any,str|None]:
    if not channel.get("base_url"): return False,None,"Base URL未配置"
    path={"jmapi":"/jmapi/status","libtv":"/libtv/api/v1/video/balances","grsai":"/v1/draw/result","local_h3":"/health","mxapi":"/api/v2/music/task?id=0","runninghub":"/openapi/v2/query","teamorouter":"/v1/models"}.get(channel["adapter"],"/health")
    try:
        with httpx.Client(timeout=min(channel["timeout_seconds"],20),follow_redirects=False) as client:
            if channel["adapter"]=="grsai": response=client.post(urljoin(channel["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(channel),json={"id":"connection-test"})
            elif channel["adapter"]=="runninghub": response=client.post(urljoin(channel["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(channel),json={"taskId":"0"})
            else: response=client.get(urljoin(channel["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(channel))
        if channel["adapter"]=="runninghub":
            # 探针查一个不存在的 taskId：这是上游唯一「必定被拒、必定零花费」的入口
            # （字段校验先于建任务），响应体恒为 HTTP 200 的扁平 JSON。
            # 2026-09-29 实测这个入口**只校验 Authorization 头在不在，不校验它对不对** ——
            # 错 token 与对 token 返回逐字相同。所以能读出来的只有「网关通 + 头带了」，
            # 读不出「token 有效」；缺头时上游回 {"code":1602,"msg":"HEADER_API_KEY_NOT_FOUND"}。
            # 这是已知的弱判据，跟 mxapi 那条一个道理：能证伪，不能证实。
            try: body=response.json()
            except ValueError: body={}
            if isinstance(body,dict) and int(body.get("code") or 0)==1602: return False,body,"HEADER_API_KEY_NOT_FOUND"
            if response.status_code>=400: return False,None,f"HTTP {response.status_code}"
            return True,body,None
        if channel["adapter"]=="teamorouter":
            # GET /v1/models 是免费且**真的鉴权**的入口：错 token 回 401、不带头也回 401
            # （2026-09-30 实测）。所以这条比 RunningHub/mxapi 的弱判据强 —— 它同时证明了
            # 「网关通」和「key 有效」，是三条第三方渠道里唯一能证实的一条。
            if response.status_code in {401,403}: return False,None,"HTTP %d：API key 无效或未配置"%response.status_code
            if response.status_code>=400: return False,None,f"HTTP {response.status_code}"
            try: return True,response.json(),None
            except ValueError: return True,{"status_code":response.status_code},None
        if channel["adapter"]=="mxapi":
            # 这个渠道连「查一个不存在的任务」都要过鉴权，所以 401/403 是「token 不认」的指纹；
            # 400/404 之类的 JSON 错误体反而说明网关通、鉴权过了（上游用 code 字段报应用层错误，
            # 不一定要用 HTTP 状态码）。5xx 与超时按离线处理——2026-09-24 上游 /api/v2/music/*
            # 全路径 nginx 502（营销首页正常），这里就会如实报「HTTP 502」。
            if response.status_code in {401,403,500,502,503,504}: return False,None,f"HTTP {response.status_code}"
            try: return True,response.json(),None
            except ValueError: return True,{"status_code":response.status_code},None
        if response.status_code>=400:return False,None,f"HTTP {response.status_code}"
        return True,response.json(),None
    except Exception as exc:return False,None,type(exc).__name__


def _sync_image_results(
    binding: dict[str, Any], body: dict[str, Any], request: dict[str, Any], job_id: str
) -> list[str]:
    """同步生图端点的内联结果 → 可交付的 https 链接。

    OpenAI 兼容的 `POST /v1/images/generations` 把成品直接塞在响应体里
    （`data[i].b64_json`，PNG 的 base64，实测 1K 一张 1.6MB），**没有 task id**。
    所以它没法走中台的「提交 → 轮询」链路，只能在提交返回时就地收下。

    b64 必须落盘再传内核：作业契约里 `result_urls` 是链接而不是内联字节，
    而且把 MB 级 base64 原样存进 generation_jobs 会把库撑爆。
    """
    items = body.get("data")
    if not isinstance(items, list):
        return []
    entries = [item for item in items if isinstance(item, dict)]
    if not entries:
        return []
    if not any(item.get("b64_json") for item in entries):
        # 上游改回 URL 模式（或换了个不吃 b64 的型号）时不必下载转存，直接透传。
        return list(dict.fromkeys(_urls_from_container(entries)))
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("inline image results require kernel_upload_url to be configured")
    directory = settings.video_generation_work_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    persisted: list[str] = []
    try:
        for index, entry in enumerate(entries, start=1):
            encoded = entry.get("b64_json")
            if not encoded:
                persisted.extend(_urls_from_container(entry))
                continue
            path = directory / f"result-{index}.png"
            path.write_bytes(base64.b64decode(encoded))
            data = {
                "external_ref": request.get("external_ref") or job_id,
                "run_id": "",
                "campaign_id": "",
                "project_id": "",
                "stage": f"media.image_generation.result_{index}",
                "actor": "video-generation-worker",
            }
            with path.open("rb") as stream:
                response = httpx.post(
                    settings.kernel_upload_url,
                    headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
                    data=data,
                    files={"file": (path.name, stream, "image/png")},
                    timeout=httpx.Timeout(
                        settings.video_generation_upload_timeout_seconds, connect=30
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
    if request.get("model") in OSS_PERSISTED_MODELS:
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
    # 进度记在自己的 store 里（不是 Celery state），所以排队状态也写那边。
    with job_slot("video_generation", on_wait=lambda limit: _store().update_job(job_id, stage="waiting_slot")):
        return _generate(self,job_id)


def _generate(self,job_id:str)->dict[str,Any]:
    settings = get_settings()
    store=_store(); job=store.job(job_id,include_request=True); req=job["request"]; started=time.monotonic()
    requested=req.get("channel") or "jmapi"; order=[requested] if requested!="auto" else store.binding_channels(req["model"])
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
            if binding["adapter"]=="teamorouter":
                # 同步端点：响应体里就已经有成品，没有 task id，所以下面的轮询分支对它是死路——
                # `if not tid` 会把它判成「上游没给任务号」而失败，而**钱已经花了**。
                # 先落 submitted=True：转存失败属于「图已经生成、我们没接住」，这种情况不能
                # 让 auto 换渠道重来（那会再买一张），只能如实失败。
                submitted = True
                urls = _sync_image_results(binding, body, req, job_id)
                if not urls:
                    raise RuntimeError(
                        f"upstream returned no image: "
                        f"{_error_detail(body, 'no image in response')[:160]}"
                    )
                return _finish_success(store,job_id,req,"","completed",urls,started)
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
                        elif binding["adapter"]=="runninghub": q=client.post(urljoin(binding["base_url"].rstrip("/")+"/",path.lstrip("/")),headers=_headers(binding),json={"taskId":tid})
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
