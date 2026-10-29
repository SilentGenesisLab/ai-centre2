"""文本生成（火山方舟 / 阿里云百炼 DeepSeek、Qwen Omni）。

平台原有的四类能力里没有自由文本生成：`decision_generation` 回的是结构化判定，
不是可发布的内容。这里是第一种 `text_generation`，走 OpenAI 兼容的
`/chat/completions`（同步返回，没有 task id、没有媒体成品）。

## 语音输出（Qwen Omni）

`output_audio=true` 时额外要语音。**只能走流式**：2026-10-28 实测，非流式请求即使带上
`modalities:["text","audio"]`，响应体的 `message.audio` 也是 null，而 usage 里照样记
`audio_tokens` —— 也就是「按音频计费但拿不到音频」。所以这一支固定发 `stream:true`，
在 SSE 里同时收 `delta.content` 与 `delta.audio.data`。

上游给的是**裸 PCM**（int16 LE / 单声道 / 24kHz），不是 RIFF 容器 —— 即使请求里写的是
`format:"wav"`。所以这里自己包 WAV 头，再转 MP3 交付（MP3 才打得进 AIGC 隐式标识，
`provenance.detect_kind` 至今认不出 WAV）。

## 标识

文本没有容器，所以隐式标识写在**字符本身**（Unicode 标签字符，肉眼不可见），
显式标识是一条可见的尾注。两者都由请求的 `explicit_label` / 默认策略决定，
具体实现见 `provenance.embed_text_provenance`。语音按平台约定**只做隐式**。
"""

from __future__ import annotations

import base64
import json
import shutil
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .celery_app import celery_app
from .concurrency import job_slot
from .generation_tasks import (
    _cancel_requested,
    _compatible,
    _error_detail,
    _headers,
    _response_body,
    _store,
)
from .provenance import apply_provenance, build_aigc_payload, embed_text_provenance

# Omni 语音输出的采样参数。上游文档与实测一致：16 位、单声道、24kHz，且没有容器头。
OMNI_PCM_RATE = 24000
OMNI_PCM_WIDTH = 2
OMNI_PCM_CHANNELS = 1


def _payload(binding: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 兼容的 chat completions。型号用绑定的 upstream_model。"""
    messages: list[dict[str, str]] = []
    if request.get("system"):
        messages.append({"role": "system", "content": request["system"]})
    messages.append({"role": "user", "content": request["prompt"]})
    payload: dict[str, Any] = {
        "model": binding["upstream_model"],
        "messages": messages,
        "max_tokens": int(request.get("max_tokens") or 1024),
    }
    # temperature 允许为 0（确定性），所以判 None 而不是判真假。
    if request.get("temperature") is not None:
        payload["temperature"] = float(request["temperature"])
    if request.get("output_audio"):
        payload["modalities"] = ["text", "audio"]
        payload["audio"] = {"voice": request.get("voice") or "Tina", "format": "wav"}
        # 必须流式，理由见模块 docstring：非流式拿不到音频却照样计费。
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    # 推理开关。DeepSeek 这类带 reasoning_content 的型号会把 max_tokens 先花在推理上，
    # 推理没结束就把预算烧光 → 返回 `finish_reason="length"` 且 `content=""`，
    # 表现为「文本通道间歇性返回空」。实测两个渠道都认 `thinking:{"type":"disabled"}`，
    # 关掉之后 content 完整、延迟 7.2s→4.0s、token 约减半。
    # 只在绑定声明了 thinking_control 时才发这个字段 —— 别的型号不认，发了可能报错。
    if binding.get("capabilities", {}).get("thinking_control"):
        # 请求显式要推理时才不发（不发 == 用上游默认，也就是开着）。
        if not request.get("thinking"):
            payload["thinking"] = {"type": "disabled"}
    return payload


def _text(body: dict[str, Any]) -> str | None:
    """从 chat completions 响应里取正文。

    结构不对就返回 None —— 调用方据此报「上游没给文本」，而不是把空串当成成功
    （空串会让人以为模型只回了一个空回答）。
    """
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) and content.strip() else None


def _no_text_reason(body: dict[str, Any]) -> str:
    """正文为空时给出**可执行**的原因，而不是笼统的「没有 choices」。

    这条曾经是最误导人的一处：DeepSeek 把 max_tokens 全花在推理上时，
    choices 明明在、content 是空串，而报错说「响应里没有 choices」，
    排查方向直接被带偏。所以这里按实际形态分三种说法。
    """
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return "上游响应里没有 choices"
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    finish = first.get("finish_reason")

    if isinstance(reasoning, str) and reasoning.strip():
        tokens = ((body.get("usage") or {}).get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        )
        detail = f"，推理用了 {tokens} token" if tokens else ""
        hint = (
            "提高 max_tokens，或传 thinking=false 关掉推理"
            if finish == "length"
            else "可传 thinking=false 关掉推理"
        )
        return f"正文为空、推理内容非空（finish_reason={finish}{detail}）。{hint}"
    if isinstance(content, str):
        if finish == "length":
            return f"正文被截断成空（finish_reason=length）。提高 max_tokens"
        return "上游回了空正文"
    return "上游响应里没有 message.content"


def _decode_audio_parts(parts: list[str]) -> bytes:
    """把 `delta.audio.data` 的分片还原成 PCM 字节。

    实测（2026-10-28）上游给的是**同一串 base64 的等长切片**：一次 26 块，每块长度都是
    4 的倍数、都不含 `=`。这种切片直接拼字符串再解一次也对，但那样会把「切片正好落在
    4 字符组中间」变成解码错误。所以这里累计到 4 的倍数就解一次，两种框法（连续流的切片
    / 各自独立编码的分片）都还原得对；只认一种的话，上游哪天换了切法就会解出偏移的音频。
    """
    buffer = ""
    decoded: list[bytes] = []
    for part in parts:
        buffer += part
        if "=" in buffer:
            # 带填充 = 这一块是独立编码的，立刻收口。
            decoded.append(base64.b64decode(buffer))
            buffer = ""
            continue
        cut = len(buffer) - len(buffer) % 4
        if cut:
            decoded.append(base64.b64decode(buffer[:cut]))
            buffer = buffer[cut:]
    if buffer:
        decoded.append(base64.b64decode(buffer + "=" * (-len(buffer) % 4)))
    return b"".join(decoded)


def _consume_sse(lines: Any, model: str) -> dict[str, Any]:
    """把 SSE 行流(OpenAI chat delta)收成一份 chat-completion 形状的结果。

    拆成纯函数是为了能测：传输层（httpx 的 stream）不进这里，喂一串字符串就能验。
    """
    text_parts: list[str] = []
    audio_parts: list[str] = []
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    resolved = model

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            raise RuntimeError(
                f"upstream rejected request: {_error_detail(event, 'request rejected')[:160]}"
            )
        if event.get("usage"):
            usage = event["usage"]
        if event.get("model"):
            resolved = str(event["model"])
        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            text_parts.append(delta["content"])
        audio = delta.get("audio")
        if isinstance(audio, dict) and audio.get("data"):
            audio_parts.append(str(audio["data"]))

    text = "".join(text_parts)
    if not text.strip():
        raise RuntimeError(
            f"upstream returned no text (finish_reason={finish_reason or 'unknown'})"
        )
    if not audio_parts:
        # 真发生了说明上游改了契约。这时**必须报错**：静默返回纯文本会让调用方
        # 以为「语音生成成功了但没给」，而那一单已经按 audio_tokens 计过费。
        raise RuntimeError(
            "upstream returned text but no audio chunks; audio output is not available "
            f"for this model (finish_reason={finish_reason or 'unknown'})"
        )

    pcm = _decode_audio_parts(audio_parts)
    return {
        "model": resolved,
        "usage": usage,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "_audio": {
            "pcm": pcm,
            "duration_seconds": round(
                len(pcm) / (OMNI_PCM_WIDTH * OMNI_PCM_CHANNELS * OMNI_PCM_RATE), 2
            ),
        },
    }


def _stream_with_audio(
    binding: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    """发一个流式请求，把文本与语音都收齐。

    返回值形如 chat completion，多出来的 `_audio` 键放着裸 PCM 与时长；
    做成这个形状是为了让下游的取模、标识、写库逻辑原样复用。
    """
    with httpx.Client(
        timeout=httpx.Timeout(binding["timeout_seconds"], connect=20),
        follow_redirects=False,
    ) as client:
        with client.stream(
            "POST",
            urljoin(binding["base_url"].rstrip("/") + "/", binding["submit_path"].lstrip("/")),
            headers=_headers(binding),
            json=_payload(binding, request),
        ) as response:
            if response.status_code >= 400:
                # 流式响应出错时 body 还没读，要先读完才能拿到错误详情。
                response.read()
                try:
                    error_body = _response_body(response)
                except ValueError:
                    error_body = {}
                raise RuntimeError(
                    f"upstream HTTP {response.status_code}: "
                    f"{_error_detail(error_body, 'request rejected')[:160]}"
                )
            return _consume_sse(response.iter_lines(), str(binding["upstream_model"]))


def _write_wav(pcm: bytes, path: Path) -> None:
    """把裸 PCM 包成 WAV；万一上游哪天真的给了 RIFF，就原样落盘。"""
    if pcm[:4] == b"RIFF":
        path.write_bytes(pcm)
        return
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(OMNI_PCM_CHANNELS)
        handle.setsampwidth(OMNI_PCM_WIDTH)
        handle.setframerate(OMNI_PCM_RATE)
        handle.writeframes(pcm)


def _persist_audio(
    pcm: bytes, request: dict[str, Any], job_id: str, settings: Any
) -> str:
    """PCM → WAV → MP3 → 打标识 → 传对象存储，返回 https 链接。

    与 `audio_generation_tasks._persist_audio_results` 是同一条路（那边的输入是上游给的
    URL、这里是我们自己拼的字节，所以没法直接复用），转码与上传的形状保持一致。
    """
    # 本地导入：audio_generation_tasks 会拉起它自己的依赖，这条链路用不到就别在模块导入时付。
    from .audio_generation_tasks import _to_mp3

    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("audio output requires kernel_upload_url to be configured")
    directory = settings.audio_generation_work_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    try:
        wav_path = directory / "result-1.wav"
        _write_wav(pcm, wav_path)
        upload_path = _to_mp3(wav_path)
        # 语音只做隐式标识（音频上没有公认的可见标识做法）。
        apply_provenance(
            upload_path,
            job_id=job_id,
            external_ref=request.get("external_ref"),
            explicit_label=False,
            settings=settings,
            work_dir=directory,
        )
        data = {
            "external_ref": request.get("external_ref") or job_id,
            "run_id": "",
            "campaign_id": "",
            "project_id": "",
            "stage": "media.text_generation.audio",
            "actor": "text-generation-worker",
        }
        with upload_path.open("rb") as stream:
            response = httpx.post(
                settings.kernel_upload_url,
                headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
                data=data,
                files={"file": (upload_path.name, stream, "audio/mpeg")},
                timeout=httpx.Timeout(
                    settings.tts_upload_timeout_seconds, connect=30
                ),
            )
        response.raise_for_status()
        url = str(response.json().get("uri") or "")
        if not url.startswith("https://"):
            raise RuntimeError("kernel upload did not return an HTTPS URL")
        return url
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _mark(text: str, request: dict[str, Any], job_id: str, settings: Any) -> tuple[str, dict[str, Any]]:
    """给文本打标：隐式恒加，显式按请求。

    顺序有讲究：**先拼可见尾注、再写不可见载荷**，这样显式那段落在正文与隐式标记之间，
    读回时 `strip_text_provenance` 摘掉的只有不可见部分，可见尾注还在。
    """
    payload, missing = build_aigc_payload(
        job_id=job_id,
        external_ref=request.get("external_ref"),
        filename=f"{job_id}.txt",
        settings=settings,
    )
    if request.get("explicit_label"):
        text = f"{text}\n\n{settings.aigc_text_label}"
    text = embed_text_provenance(text, payload)
    return text, {"implicit": True, "explicit": bool(request.get("explicit_label")), "missing_fields": missing}


@celery_app.task(
    bind=True, name="control_plane.text_generation", time_limit=900, soft_time_limit=840
)
def generate_text(self, job_id: str) -> dict[str, Any]:
    with job_slot(
        "text_generation",
        on_wait=lambda limit: _store().update_job(job_id, stage="waiting_slot"),
    ):
        return _generate_text(job_id)


def _generate_text(job_id: str) -> dict[str, Any]:
    store = _store()
    job = store.job(job_id, include_request=True)
    request = job["request"]
    started = time.monotonic()
    requested = request.get("channel") or "auto"
    order = store.channel_order(request["model"], requested)
    store.update_job(
        job_id, status="running", stage="selecting_channel",
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
        answered = False
        try:
            store.update_job(
                job_id, channel_id=binding["channel_id"], stage="submitting", fallback_count=index
            )
            if request.get("output_audio"):
                # 语音只能走流式（见模块 docstring）：非流式上游照样按 audio_tokens 计费，
                # 却不返回 audio 字段。这一支自己发请求、自己收 SSE。
                body = _stream_with_audio(binding, request)
            else:
                with httpx.Client(
                    timeout=httpx.Timeout(binding["timeout_seconds"], connect=20),
                    follow_redirects=False,
                ) as client:
                    response = client.post(
                        urljoin(
                            binding["base_url"].rstrip("/") + "/",
                            binding["submit_path"].lstrip("/"),
                        ),
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
                if body.get("error"):
                    raise RuntimeError(
                        f"upstream rejected request: "
                        f"{_error_detail(body, 'request rejected')[:160]}"
                    )
            text = _text(body)
            if text is None:
                raise RuntimeError(f"upstream returned no text: {_no_text_reason(body)}")
            # 文本到手。后面的写库失败**不能再换渠道重问**：每调一次都重新计费，
            # 而且同一 prompt 两次生成未必一样，手上这份比换一家更值钱。
            answered = True
            settings = _settings()
            audio = body.get("_audio") if isinstance(body.get("_audio"), dict) else None
            audio_info: dict[str, Any] | None = None
            if audio is not None:
                store.update_job(job_id, stage="uploading_audio")
                audio_info = {
                    "url": _persist_audio(audio["pcm"], request, job_id, settings),
                    "duration_seconds": audio.get("duration_seconds"),
                    "voice": request.get("voice") or "Tina",
                    "format": "mp3",
                    "source_format": "pcm_s16le_24000_mono",
                }
            marked, report = _mark(text, request, job_id, settings)
            response_payload: dict[str, Any] = {
                "model": str(body.get("model") or binding["upstream_model"]),
                # text 是**打过标的最终文本**；plain 是可见原文（摘掉不可见部分），
                # 方便调用方直接展示或做后续处理而不用自己解。
                "text": marked,
                "plain": text,
                "aigc": report,
                "usage": body.get("usage"),
            }
            if audio_info is not None:
                response_payload["audio"] = audio_info
            store.update_job(
                job_id,
                upstream_task_id="",
                upstream_response_json=json.dumps(response_payload, ensure_ascii=False),
                result_urls_json=json.dumps([audio_info["url"]] if audio_info else []),
                error=None,
            )
            if _cancel_requested(store, job_id):
                store.update_job(job_id, status="cancelled", stage="cancelled")
                return store.job(job_id)
            store.update_job(
                job_id,
                status="succeeded",
                stage="completed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            return store.job(job_id)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            failures.append(f"{code}:{type(exc).__name__}")
            continue
        except Exception as exc:
            failures.append(f"{code}:{str(exc)[:200]}")
            if answered:
                break
            continue
    store.update_job(
        job_id,
        status="failed",
        stage="failed",
        error="; ".join(failures) or "no enabled compatible channel",
        finished_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return store.job(job_id)


def _settings():
    from .config import get_settings

    return get_settings()
