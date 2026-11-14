from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .llm_monitor import LlmMonitor, LlmProbeStore, parse_models
from .observability_api import verify_internal_token

router = APIRouter(prefix="/internal/admin/llm-probe", include_in_schema=False)


@lru_cache(maxsize=1)
def get_llm_probe_store() -> LlmProbeStore:
    settings = get_settings()
    return LlmProbeStore(
        settings.llm_probe_db_path,
        parse_models(settings.llm_probe_models),
    )


def get_llm_monitor() -> LlmMonitor:
    return LlmMonitor(get_llm_probe_store(), get_settings())


class RunProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=128)


def _webhook_host() -> str | None:
    from urllib.parse import urlsplit

    webhook = get_settings().llm_probe_webhook_url
    if not webhook:
        return None
    parsed = urlsplit(webhook.get_secret_value())
    return parsed.hostname


@router.get("/status", dependencies=[Depends(verify_internal_token)])
async def probe_status(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    settings = get_settings()
    result = get_llm_probe_store().status(days)
    result.update(
        enabled=bool(settings.llm_probe_enabled and settings.llm_probe_api_key),
        base_url=settings.llm_probe_base_url,
        models=list(parse_models(settings.llm_probe_models)),
        webhook_configured=bool(settings.llm_probe_webhook_url),
        webhook_host=_webhook_host(),
        interval_seconds=settings.llm_probe_interval_seconds,
        retry_seconds=settings.llm_probe_retry_seconds,
        failure_threshold=settings.llm_probe_failure_threshold,
    )
    return result


@router.post("/run-check", dependencies=[Depends(verify_internal_token)])
async def run_probe(request: RunProbeRequest) -> dict[str, Any]:
    return await get_llm_monitor().run(request.model)


@router.post("/test-notification", dependencies=[Depends(verify_internal_token)])
async def test_notification() -> dict[str, bool]:
    try:
        get_llm_monitor().test_notification()
        return {"ok": True}
    except Exception as exc:
        get_llm_probe_store().delivery(None, "test", False, str(exc))
        raise HTTPException(status_code=502, detail="飞书测试通知发送失败") from exc
