from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .health_monitor import DEFAULT_TARGETS, HealthMonitor, HealthMonitorStore
from .observability_api import verify_internal_token

router = APIRouter(prefix="/internal/admin/health-monitor", include_in_schema=False)


@lru_cache(maxsize=1)
def get_health_monitor_store() -> HealthMonitorStore:
    settings = get_settings()
    secret = settings.health_monitor_key.get_secret_value() if settings.health_monitor_key else f"{settings.service_token}:health-monitor"
    urls = dict((item[0], item) for item in DEFAULT_TARGETS)
    urls["asr"] = ("asr", "语音识别", f"{settings.asr_backend_url.rstrip('/')}/health")
    urls["tts"] = ("tts", "语音合成", f"{settings.tts_backend_url.rstrip('/')}/health")
    urls["musetalk"] = ("musetalk", "唇形驱动/GFPGAN", f"{settings.musetalk_backend_url.rstrip('/')}/health")
    urls["ocr"] = ("ocr", "OCR文字识别", f"{settings.ocr_gateway_url.rstrip('/')}/health")
    urls["subtitle"] = ("subtitle", "字幕检测", f"{settings.subtitle_api_url.rstrip('/')}/health")
    urls["face"] = ("face", "人脸处理", f"{settings.face_api_url.rstrip('/')}/health")
    urls["speaker"] = ("speaker", "说话人检测", f"{settings.speaker_verify_url.rstrip('/')}/health")
    urls["emotion"] = ("emotion", "情绪检测", f"{settings.emotion_verify_url.rstrip('/')}/health")
    return HealthMonitorStore(settings.health_monitor_db_path, secret, tuple(urls.values()))


def get_health_monitor() -> HealthMonitor:
    return HealthMonitor(get_health_monitor_store(), get_settings())


class NotificationConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    webhook_url: str | None = Field(default=None, max_length=2048)
    secret: str | None = Field(default=None, max_length=512)
    clear: bool = False
    clear_secret: bool = False


class RunCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str = Field(default="l1", pattern="^(l1|l2|paid)$")
    target_id: str | None = Field(default=None, max_length=64)


@router.get("/targets", dependencies=[Depends(verify_internal_token)])
async def targets() -> dict[str, Any]:
    return {"items": get_health_monitor_store().targets()}


@router.get("/status", dependencies=[Depends(verify_internal_token)])
async def monitor_status(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    return get_health_monitor_store().status(days)


@router.get("/history", dependencies=[Depends(verify_internal_token)])
async def history(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    return get_health_monitor_store().status(days)


@router.get("/incidents", dependencies=[Depends(verify_internal_token)])
async def incidents(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    result = get_health_monitor_store().status(days)
    return {"items": result["incidents"], "days": days}


@router.get("/config", dependencies=[Depends(verify_internal_token)])
async def config() -> dict[str, Any]:
    return get_health_monitor_store().public_config()


@router.put("/config", dependencies=[Depends(verify_internal_token)])
async def update_config(request: NotificationConfigRequest) -> dict[str, Any]:
    try:
        return get_health_monitor_store().save_config(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/test-notification", dependencies=[Depends(verify_internal_token)])
async def test_notification() -> dict[str, bool]:
    try:
        get_health_monitor().test_notification()
        return {"ok": True}
    except Exception as exc:
        get_health_monitor_store().delivery(None, "test", False, str(exc))
        raise HTTPException(status_code=502, detail="飞书测试通知发送失败") from exc


@router.post("/run-check", dependencies=[Depends(verify_internal_token)])
async def run_check(request: RunCheckRequest) -> dict[str, Any]:
    if request.level != "l1":
        return await get_health_monitor().run_l2(paid=request.level == "paid")
    return await get_health_monitor().run_l1(request.target_id)
