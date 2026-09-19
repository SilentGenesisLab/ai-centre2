from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .h3_store import H3Store
from .observability import ObservabilityStore


router = APIRouter(prefix="/internal/admin/observability", include_in_schema=False)


def verify_internal_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {get_settings().service_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")


@lru_cache(maxsize=1)
def get_observability_store() -> ObservabilityStore:
    settings = get_settings()
    payload_secret = (
        settings.observability_payload_key.get_secret_value()
        if settings.observability_payload_key
        else f"{settings.service_token}:observability-payload"
    )
    return ObservabilityStore(
        settings.observability_db_path,
        payload_secret,
        f"{settings.service_token}:observability-fingerprint",
        settings.observability_payload_retention_days,
        settings.observability_record_retention_days,
    )


class PricingRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=2, max_length=32)
    operation: str = Field(default="*", min_length=1, max_length=64)
    unit_type: str = Field(min_length=2, max_length=32)
    fixed_fee: str | float = Field(default="0")
    unit_price: str | float = Field(default="0")
    effective_from: str | None = None


@router.get("/health", dependencies=[Depends(verify_internal_token)])
async def observability_health() -> dict[str, Any]:
    return get_observability_store().health()


@router.get("/overview", dependencies=[Depends(verify_internal_token)])
async def observability_overview(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    service: str | None = None,
) -> dict[str, Any]:
    try:
        return get_observability_store().overview(from_date, to_date, service)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/summary", dependencies=[Depends(verify_internal_token)])
async def observability_analytics_summary(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    service: str | None = None,
    operation: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    try:
        return get_observability_store().analytics_summary(
            from_date, to_date, service, operation, status_filter
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/timeseries", dependencies=[Depends(verify_internal_token)])
async def observability_analytics_timeseries(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    service: str | None = None,
    operation: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    try:
        return get_observability_store().analytics_timeseries(
            from_date, to_date, service, operation, status_filter
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/stages", dependencies=[Depends(verify_internal_token)])
async def observability_analytics_stages(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    service: str | None = None,
    operation: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    try:
        return get_observability_store().analytics_stages(
            from_date, to_date, service, operation, status_filter
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/h3-duration", dependencies=[Depends(verify_internal_token)])
async def observability_h3_duration_analytics(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    machine_type: str | None = None,
    resolution: str | None = None,
    input_mode: str | None = None,
    segment_mode: str | None = None,
    duration_min: float = Query(default=4, ge=1, le=30),
    duration_max: float = Query(default=15, ge=1, le=30),
) -> dict[str, Any]:
    if duration_max < duration_min:
        raise HTTPException(status_code=422, detail="duration_max must be >= duration_min")
    try:
        start, end = get_observability_store()._window(from_date, to_date)
        result = H3Store(get_settings().h3_db_path).duration_analytics(
            start, end,
            machine_type=machine_type,
            resolution=resolution,
            input_mode=input_mode,
            segment_mode=segment_mode,
            duration_min=duration_min,
            duration_max=duration_max,
        )
        result["submission_latency_ms"] = get_observability_store().analytics_summary(
            from_date, to_date, "h3", "generate", None
        )["api"]["latency_ms"]
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/calls", dependencies=[Depends(verify_internal_token)])
async def observability_calls(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    trace_id: str | None = Query(default=None, max_length=128),
    task_id: str | None = Query(default=None, max_length=128),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    try:
        return get_observability_store().list_calls(
            page=page,
            page_size=page_size,
            service=service,
            status=status_filter,
            trace_id=trace_id,
            task_id=task_id,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/calls/{call_id}", dependencies=[Depends(verify_internal_token)])
async def observability_call_detail(call_id: str) -> dict[str, Any]:
    result = get_observability_store().call_detail(call_id)
    if result is None:
        raise HTTPException(status_code=404, detail="call not found")
    return result


@router.get("/calls/{call_id}/reveal", dependencies=[Depends(verify_internal_token)])
async def observability_call_reveal(call_id: str) -> dict[str, Any]:
    result = get_observability_store().reveal(call_id)
    if result is None:
        raise HTTPException(status_code=404, detail="request/response snapshot not found or expired")
    return result


@router.get("/tasks", dependencies=[Depends(verify_internal_token)])
async def observability_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    task_id: str | None = Query(default=None, max_length=128),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    try:
        return get_observability_store().list_tasks(
            page=page,
            page_size=page_size,
            service=service,
            status=status_filter,
            task_id=task_id,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tasks/{task_id}", dependencies=[Depends(verify_internal_token)])
async def observability_task_detail(task_id: str) -> dict[str, Any]:
    result = get_observability_store().task_detail(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    return result


@router.get("/pricing-rules", dependencies=[Depends(verify_internal_token)])
async def observability_pricing_rules() -> dict[str, Any]:
    return {"items": get_observability_store().list_pricing_rules(), "currency": "CNY"}


@router.post("/pricing-rules", dependencies=[Depends(verify_internal_token)])
async def create_observability_pricing_rule(request: PricingRuleRequest) -> dict[str, Any]:
    try:
        return get_observability_store().create_pricing_rule(request.model_dump(exclude_none=True))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/export.csv", dependencies=[Depends(verify_internal_token)])
async def export_observability_calls(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    service: str | None = None,
) -> Response:
    try:
        content = get_observability_store().export_calls_csv(from_date, to_date, service)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ai-centre-api-calls.csv"},
    )


@router.post("/maintenance/cleanup", dependencies=[Depends(verify_internal_token)])
async def cleanup_observability() -> dict[str, int]:
    return get_observability_store().cleanup()
