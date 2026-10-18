"""文本生成（火山方舟 / DeepSeek flash）。

平台原有的四类能力里没有自由文本生成：`decision_generation` 回的是结构化判定，
不是可发布的内容。这里是第一种 `text_generation`，走 Ark 的 OpenAI 兼容
`/chat/completions`（同步返回，没有 task id、没有媒体成品）。

## 标识

文本没有容器，所以隐式标识写在**字符本身**（Unicode 标签字符，肉眼不可见），
显式标识是一条可见的尾注。两者都由请求的 `explicit_label` / 默认策略决定，
具体实现见 `provenance.embed_text_provenance`。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
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
from .provenance import build_aigc_payload, embed_text_provenance


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
            with httpx.Client(
                timeout=httpx.Timeout(binding["timeout_seconds"], connect=20),
                follow_redirects=False,
            ) as client:
                response = client.post(
                    urljoin(
                        binding["base_url"].rstrip("/") + "/", binding["submit_path"].lstrip("/")
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
                raise RuntimeError(
                    f"upstream returned no text: "
                    f"{_error_detail(body, 'upstream response did not contain choices')[:160]}"
                )
            # 文本到手。后面的写库失败**不能再换渠道重问**：每调一次都重新计费，
            # 而且同一 prompt 两次生成未必一样，手上这份比换一家更值钱。
            answered = True
            settings = _settings()
            marked, report = _mark(text, request, job_id, settings)
            store.update_job(
                job_id,
                upstream_task_id="",
                upstream_response_json=json.dumps(
                    {
                        "model": str(body.get("model") or binding["upstream_model"]),
                        # text 是**打过标的最终文本**；plain 是可见原文（摘掉不可见部分），
                        # 方便调用方直接展示或做后续处理而不用自己解。
                        "text": marked,
                        "plain": text,
                        "aigc": report,
                        "usage": body.get("usage"),
                    },
                    ensure_ascii=False,
                ),
                result_urls_json=json.dumps([]),
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
