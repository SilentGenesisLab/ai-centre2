"""计费中心的管理接口：总览、明细、导出、费率、平台总闸。

鉴权不在这里声明——由 api.py 里 `app.include_router(..., dependencies=[...])` 统一挂，
否则这个模块要反向 import api.py，绕成循环。
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .api_keys import ApiKeyStore
from .credit_pricing import MICRO, from_yuan

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 与其它 /internal/admin/* 一致：内部接口不进公开文档（tests/test_openapi.py 钉着公开
# 端点只能有固定的那组中文分类）。
router = APIRouter(prefix="/internal/admin/billing", tags=["计费"], include_in_schema=False)


def _store() -> ApiKeyStore:
    from .api import get_api_key_store

    return get_api_key_store()


def _bound(value: str | None, *, exclusive_end: bool = False) -> str | None:
    """北京时间入参 → 库里存着的 UTC ISO 串。

    只给日期（2026-11-05）时，起点取当天零点、终点取次日零点（不含）。
    """
    if not value:
        return None
    text = value.strip()
    day_only = len(text) == 10
    if day_only:
        text = f"{text}T00:00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid datetime: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    if exclusive_end and day_only:
        moment += timedelta(days=1)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _filters(start: str | None, end: str | None, service: str | None, model: str | None,
             channel: str | None, state: str | None, key_id: str | None, keyword: str | None) -> dict[str, Any]:
    return {
        "start": _bound(start),
        "end": _bound(end, exclusive_end=True),
        "service": service,
        "model": model,
        "channel": channel,
        "state": state,
        "key_id": key_id,
        "keyword": keyword,
    }


@router.get("/summary")
async def billing_summary(
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    bounds = {"start": _bound(start), "end": _bound(end, exclusive_end=True)}
    return await asyncio.to_thread(lambda: _store().summary(**bounds))


@router.get("/charges")
async def billing_charges(
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
    service: str | None = None,
    model: str | None = None,
    channel: str | None = None,
    state: str | None = None,
    key_id: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    where = _filters(start, end, service, model, channel, state, key_id, keyword)
    return await asyncio.to_thread(
        lambda: _store().charges(page=page, page_size=page_size, **where)
    )


def _rows(where: dict[str, Any], limit: int = 20_000) -> list[dict[str, Any]]:
    store = _store()
    items: list[dict[str, Any]] = []
    page = 1
    while len(items) < limit:
        batch = store.charges(page=page, page_size=500, **where)["items"]
        items.extend(batch)
        if len(batch) < 500:
            break
        page += 1
    return items[:limit]


@router.get("/export.csv")
async def billing_export(
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
    service: str | None = None,
    model: str | None = None,
    channel: str | None = None,
    state: str | None = None,
    key_id: str | None = None,
    keyword: str | None = None,
) -> Response:
    where = _filters(start, end, service, model, channel, state, key_id, keyword)
    rows = await asyncio.to_thread(_rows, where)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["北京时间", "Key", "服务", "模型", "渠道", "数量", "单位", "单价(元)", "金额(元)", "状态", "作业状态", "来源"])
    for row in rows:
        quantity = row["quantity"] or 0
        unit_price = (row["amount_micro"] / quantity) / MICRO if quantity else 0
        writer.writerow([
            datetime.fromisoformat(row["created_at"]).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            row.get("key_name") or "",
            row.get("service") or "",
            row.get("model") or "",
            row.get("channel") or "",
            quantity,
            row.get("unit") or "",
            f"{unit_price:.6f}",
            f"{row['amount_micro'] / MICRO:.6f}",
            row.get("state") or "",
            row.get("job_status") or "",
            row.get("source") or "",
        ])
    # BOM：不加的话 Excel 按 GBK 打开中文表头是乱码。
    body = "﻿" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="billing-charges.csv"'},
    )


@router.get("/rates")
async def list_rates() -> dict[str, Any]:
    return {"items": await asyncio.to_thread(_store().rates)}


class RateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    variant: str = Field(default="*", max_length=64)
    unit: str = Field(min_length=1, max_length=32)
    amount_yuan: float = Field(ge=0)
    source: str = Field(default="manual", max_length=16)
    note: str | None = Field(default=None, max_length=256)
    enabled: bool = True


@router.put("/rates")
async def upsert_rate(request: RateRequest) -> dict[str, Any]:
    store = _store()
    amount_micro = from_yuan(request.amount_yuan)
    await asyncio.to_thread(
        store.set_rate,
        request.service,
        request.model,
        request.variant,
        request.unit,
        int(amount_micro or 0),
        request.source,
        request.note,
        request.enabled,
    )
    store.invalidate_rates()
    return {"items": await asyncio.to_thread(store.rates)}


@router.delete("/rates/{service}/{model}/{variant}")
async def delete_rate(service: str, model: str, variant: str) -> dict[str, Any]:
    store = _store()
    if not await asyncio.to_thread(store.delete_rate, service, model, variant):
        raise HTTPException(status_code=404, detail="rate not found")
    store.invalidate_rates()
    return {"items": await asyncio.to_thread(store.rates)}


@router.get("/settings")
async def billing_settings() -> dict[str, Any]:
    settings = await asyncio.to_thread(_store().settings)
    return settings | {
        "platform_limit_5h": _yuan_of(settings["platform_limit_5h_micro"]),
        "platform_limit_24h": _yuan_of(settings["platform_limit_24h_micro"]),
        "platform_limit_total": _yuan_of(settings["platform_limit_total_micro"]),
    }


def _yuan_of(amount_micro: int | None) -> float | None:
    return None if amount_micro is None else round(amount_micro / MICRO, 4)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_limit_5h: float | None = Field(default=None, ge=0)
    platform_limit_24h: float | None = Field(default=None, ge=0)
    platform_limit_total: float | None = Field(default=None, ge=0)
    credits_per_yuan: float | None = Field(default=None, gt=0)


@router.put("/settings")
async def update_settings(request: SettingsRequest) -> dict[str, Any]:
    store = _store()
    changes: dict[str, Any] = {}
    for field in ("platform_limit_5h", "platform_limit_24h", "platform_limit_total"):
        if field in request.model_fields_set:
            changes[f"{field}_micro"] = from_yuan(getattr(request, field))
    if "credits_per_yuan" in request.model_fields_set:
        changes["credits_per_yuan"] = request.credits_per_yuan
    await asyncio.to_thread(store.set_settings, changes)
    settings = await asyncio.to_thread(store.settings)
    return settings | {
        "platform_limit_5h": _yuan_of(settings["platform_limit_5h_micro"]),
        "platform_limit_24h": _yuan_of(settings["platform_limit_24h_micro"]),
        "platform_limit_total": _yuan_of(settings["platform_limit_total_micro"]),
    }
