"""TeamORouter 的 Jev（TypeSafe）定型决策。

这是平台里第一个 `decision_generation` 能力类型，它和另外三类（视频/生图/音乐）都不一样：
**同步返回**（实测 1.8s、$0.000014/次）、没有 task id、没有媒体成品。一次 POST /v1/systemone
就拿到全部答案，所以这里要处理的只有「提交 → 解析 answers」，没有轮询、没有转存。

题型三种（2026-09-30 实测上游契约，含免费的参数校验探针）：

- `choice`：`criteria` 是**对象**（key → 描述），2~255 项。
  回 `{"type":"choice","choice":<key>,"confidence":f,"probabilities":{<key>:f}}`
- `score`：`criteria` 是**数组**（级别名），2~10 级。
  回 `{"type":"score","score":f,"confidence":f,"legend":{"0":<名>,...},"probabilities":{"0":f,...}}`
- `noul`：不收 `criteria`。回 `{"type":"noul","noul":f}`

注意 choice 要对象、score 要数组，**两者不能互换**（喂错了上游回
`criteria must contain 2 to N ...`）。平台能表达的题型约束比上游的校验粗，所以 `questions`
在 api 层只校验「非空字典」，**原样透传** —— 在这里替上游复刻题型校验，猜错了会挡掉本来
合法的请求，而喂错结构时上游的报错比我们编的更准。

答案不是 URL，所以落在 `upstream_response_json` 里（`result_urls` 恒为空）。
调用方读 `GET /v1/decision-generations/jobs/{id}` 的 `upstream_response.answers`。
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

MODEL = "jev"


def _payload(binding: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """上游只要这三样：型号、state（自由文本）、questions（问题字典）。"""
    return {
        "model": binding["upstream_model"],
        "state": request["state"],
        "questions": request["questions"],
    }


def _answers(body: dict[str, Any]) -> dict[str, Any] | None:
    answers = body.get("answers")
    return answers if isinstance(answers, dict) and answers else None


def _finished(store, job_id: str, started: float) -> dict[str, Any]:
    store.update_job(
        job_id,
        status="succeeded",
        stage="completed",
        finished_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return store.job(job_id)


@celery_app.task(
    bind=True, name="control_plane.decision_generation", time_limit=900, soft_time_limit=840
)
def decide(self, job_id: str) -> dict[str, Any]:
    with job_slot(
        "decision_generation",
        on_wait=lambda limit: _store().update_job(job_id, stage="waiting_slot"),
    ):
        return _decide(job_id)


def _decide(job_id: str) -> dict[str, Any]:
    store = _store()
    job = store.job(job_id, include_request=True)
    request = job["request"]
    started = time.monotonic()
    requested = request.get("channel") or "teamorouter"
    order = [requested] if requested != "auto" else store.binding_channels(request["model"])
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
            answers = _answers(body)
            if answers is None:
                raise RuntimeError(
                    f"upstream returned no answers: "
                    f"{_error_detail(body, 'upstream response did not contain answers')[:160]}"
                )
            # 答案到手了。后面的写库一旦失败，**不能再换渠道重问**：上游每次调用都重新计费，
            # 而且定型判定本来就是概率输出，第二次拿到的未必是同一份——手上这份比换一家更值钱。
            answered = True
            store.update_job(
                job_id,
                upstream_task_id="",
                upstream_response_json=json.dumps(
                    {
                        "model": str(body.get("model") or binding["upstream_model"]),
                        "answers": answers,
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
            return _finished(store, job_id, started)
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
