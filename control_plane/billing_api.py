"""计费中心的管理接口：总览、明细、导出、费率、平台总闸。

鉴权不在这里声明——由 api.py 里 `app.include_router(..., dependencies=[...])` 统一挂，
否则这个模块要反向 import api.py，绕成循环。
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .api_keys import WINDOWS, ApiKeyStore
from .credit_pricing import MICRO, from_yuan, lookup

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 与其它 /internal/admin/* 一致：内部接口不进公开文档（tests/test_openapi.py 钉着公开
# 端点只能有固定的那组中文分类）。
router = APIRouter(prefix="/internal/admin/billing", tags=["计费"], include_in_schema=False)

# 对外公开的两条：价格表与用量查询。要进接口文档，所以不能 include_in_schema=False；
# 鉴权同样由 api.py 的 include_router 统一挂（服务令牌看全平台，客户 Key 只看自己）。
public_router = APIRouter(prefix="/v1", tags=["计费与用量"])

UNIT_NAMES = {"second": "秒", "image": "张", "track": "首", "call": "次", "1000_chars": "千字"}


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
    store = _store()
    settings = await asyncio.to_thread(store.settings)
    return settings | {
        "platform_limit_5h": _yuan_of(settings["platform_limit_5h_micro"]),
        "platform_limit_24h": _yuan_of(settings["platform_limit_24h_micro"]),
        "platform_limit_total": _yuan_of(settings["platform_limit_total_micro"]),
        "promo_active": await asyncio.to_thread(store.promo_active),
    }


def _yuan_of(amount_micro: int | None) -> float | None:
    return None if amount_micro is None else round(amount_micro / MICRO, 4)


class PromoItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    unit: str = Field(default="second", max_length=32)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_limit_5h: float | None = Field(default=None, ge=0)
    platform_limit_24h: float | None = Field(default=None, ge=0)
    platform_limit_total: float | None = Field(default=None, ge=0)
    credits_per_yuan: float | None = Field(default=None, gt=0)
    # 活动促销：这些服务·型号在 promo_free_until 之前按 0 元记账（仍然记 usage，金额为 0）。
    promo_free_items: list[PromoItem] | None = None
    promo_free_until: str | None = Field(default=None, max_length=40)


@router.put("/settings")
async def update_settings(request: SettingsRequest) -> dict[str, Any]:
    store = _store()
    changes: dict[str, Any] = {}
    for field in ("platform_limit_5h", "platform_limit_24h", "platform_limit_total"):
        if field in request.model_fields_set:
            changes[f"{field}_micro"] = from_yuan(getattr(request, field))
    if "credits_per_yuan" in request.model_fields_set:
        changes["credits_per_yuan"] = request.credits_per_yuan
    if "promo_free_items" in request.model_fields_set:
        changes["promo_free_items"] = [
            item.model_dump() for item in (request.promo_free_items or [])
        ]
    if "promo_free_until" in request.model_fields_set:
        until = (request.promo_free_until or "").strip() or None
        if until is not None:
            try:
                datetime.fromisoformat(until)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"invalid datetime: {until!r}") from exc
        changes["promo_free_until"] = until
    await asyncio.to_thread(store.set_settings, changes)
    settings = await asyncio.to_thread(store.settings)
    return settings | {
        "platform_limit_5h": _yuan_of(settings["platform_limit_5h_micro"]),
        "platform_limit_24h": _yuan_of(settings["platform_limit_24h_micro"]),
        "platform_limit_total": _yuan_of(settings["platform_limit_total_micro"]),
        "promo_active": await asyncio.to_thread(store.promo_active),
    }


# ---- 对外：余额与限额 ----

@public_router.get(
    "/balance",
    summary="查询本 Key 的额度、已花金额与剩余可用",
    response_description="请求次数额度、5 小时 / 24 小时 / 累计三条金额窗口的已用与剩余（元与积分）",
)
async def balance() -> dict[str, Any]:
    """中台没有预付费钱包：额度就是这三条滚动窗口的上限，充值＝调高上限。

    服务令牌调用时没有 Key，返回平台口径（不加 key 过滤）。
    """
    from .generation_jobs import current_api_key_id

    key_id = current_api_key_id.get()
    store = _store()
    key, windows, settings = await asyncio.gather(
        asyncio.to_thread(store.key, key_id) if key_id else asyncio.sleep(0, result=None),
        asyncio.to_thread(store.usage_windows, key_id=key_id),
        asyncio.to_thread(store.settings),
    )
    per_yuan = float(settings["credits_per_yuan"])
    limits = (key or {}).get("limits") or {}

    items: dict[str, dict[str, Any]] = {}
    for window, values in windows.items():
        limit_micro = limits.get(window)
        remaining = None if limit_micro is None else limit_micro - values["amount_micro"]
        items[window] = values | {
            "limit_micro": limit_micro,
            "limit_yuan": _yuan_of(limit_micro),
            "remaining_micro": remaining,
            "remaining_yuan": _yuan_of(remaining),
            "credits": round(values["amount_micro"] / MICRO * per_yuan, 4),
        }
    return {
        "scope": "api_key" if key_id else "platform",
        "key_id": key_id,
        "key_name": (key or {}).get("name"),
        "key_prefix": (key or {}).get("prefix"),
        "request_quota": {
            "quota": (key or {}).get("quota"),
            "used": (key or {}).get("used"),
            "remaining": (key or {}).get("remaining"),
        },
        "credits_per_yuan": per_yuan,
        "windows": items,
        "top_up": "调高这个 Key 的 limit_5h / limit_24h / limit_total 即可（或加请求次数额度），中台没有预付费钱包",
    }


# ---- 对外：价格表 ----

def _channels_by_model() -> dict[str, list[dict[str, Any]]]:
    """能力目录里每个模型的渠道，按登记的优先级。定价接口只做展示，查不到就是空表。"""
    from .api import get_capability_store

    result: dict[str, list[dict[str, Any]]] = {}
    for item in get_capability_store().models():
        result[str(item["code"])] = [
            {
                "code": bind["channel_code"],
                "name": bind["channel_name"],
                "enabled": bool(bind["enabled"] and bind["channel_enabled"]),
                "health_status": bind["health_status"],
            }
            for bind in item["channels"]
        ]
    return result


def _channels_for(model: str, channels: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """费率行的 model 串 → 能力目录里的渠道。

    费率行允许通配（`kling-*` 覆盖 kling-2.x/3.x/4.x），直接拿通配串去查渠道表永远
    查不到，于是 kling-4.0 这种「提交得动」的型号在价目表里被标成 available=false。
    这里按 fnmatch 把通配串展开到真实型号上，同名渠道去重。
    单独的 `*`（服务级通配）不展开——它不代表某一款模型。
    """
    if "*" not in model:
        return channels.get(model, [])
    if model == "*":
        return []
    seen: set[str] = set()
    binds: list[dict[str, Any]] = []
    for code, entries in channels.items():
        if not fnmatchcase(code, model):
            continue
        for bind in entries:
            if bind["code"] in seen:
                continue
            seen.add(bind["code"])
            binds.append(bind)
    return binds


@public_router.get(
    "/pricing/models",
    summary="查询各个模型的计费单价与可用渠道",
    response_description="费率表：每个服务·模型·变体的单位价格（元与积分）、来源、可用渠道",
)
async def pricing_models(
    service: str | None = Query(default=None, description="按服务过滤，如 video_generation"),
    model: str | None = Query(default=None, description="按模型过滤，如 seedance-2.5"),
) -> dict[str, Any]:
    store = _store()
    rates, promo, settings = await asyncio.gather(
        asyncio.to_thread(store.rates),
        asyncio.to_thread(store.promo_rows),
        asyncio.to_thread(store.settings),
    )
    per_yuan = float(settings["credits_per_yuan"])
    channels = await asyncio.to_thread(_channels_by_model)
    # 活动促销按型号整款覆盖（promo 行不分分辨率），所以这里按 (service, model) 建索引：
    # 命中的型号原价行全部改成 0 并保留原价，客户看到的「挂牌价」就和实际扣款一致了。
    promo_models = {(str(row["service"]), str(row["model"])): row for row in promo}
    promo_until = settings.get("promo_free_until") if promo else None

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rates:
        if not row["enabled"]:
            continue
        if service and row["service"] != service:
            continue
        if model and row["model"] != model:
            continue
        seen.add((str(row["service"]), str(row["model"])))
        base_micro = int(row["amount_micro"])
        hit = promo_models.get((str(row["service"]), str(row["model"])))
        micro = 0 if hit else base_micro
        binds = _channels_for(str(row["model"]), channels)
        item = {
            "service": row["service"],
            "model": row["model"],
            "variant": row["variant"],
            "unit": row["unit"],
            "unit_name": UNIT_NAMES.get(row["unit"], row["unit"]),
            "amount_micro": micro,
            "amount_yuan": round(micro / MICRO, 6),
            "credits": round(micro / MICRO * per_yuan, 4),
            "source": "promo" if hit else row["source"],
            "note": row["note"],
            "updated_at": row["updated_at"],
            "channels": binds,
            "available": any(bind["enabled"] for bind in binds),
        }
        if hit:
            item |= {
                "original_amount_micro": base_micro,
                "original_amount_yuan": round(base_micro / MICRO, 6),
                "original_credits": round(base_micro / MICRO * per_yuan, 4),
                "promo_until": promo_until,
            }
        items.append(item)

    # 促销覆盖了某个型号、但费率表里没有它的独立行（例如只靠 `kling-*` 通配定价的
    # `minimax-h3-hs`）：补一行 0 元，原价按通配算出来，否则客户在表里根本看不到它。
    for (promo_service, promo_model), hit in promo_models.items():
        if (promo_service, promo_model) in seen:
            continue
        if service and promo_service != service:
            continue
        if model and promo_model != model:
            continue
        found = lookup(rates, promo_service, promo_model, "*")
        base_micro = found[0] if found else 0
        binds = _channels_for(promo_model, channels)
        items.append({
            "service": promo_service,
            "model": promo_model,
            "variant": "*",
            "unit": hit["unit"],
            "unit_name": UNIT_NAMES.get(hit["unit"], hit["unit"]),
            "amount_micro": 0,
            "amount_yuan": 0.0,
            "credits": 0.0,
            "source": "promo",
            "note": hit["note"],
            "updated_at": hit["updated_at"],
            "channels": binds,
            "available": any(bind["enabled"] for bind in binds),
            "original_amount_micro": base_micro,
            "original_amount_yuan": round(base_micro / MICRO, 6),
            "original_credits": round(base_micro / MICRO * per_yuan, 4),
            "promo_until": promo_until,
        })

    # 具体型号排前、服务级通配（model='*'）垫后，否则通配价会混在真模型中间看着像一款模型。
    items.sort(key=lambda item: (item["service"], item["model"] == "*", -item["amount_micro"], item["model"]))
    return {
        "credits_per_yuan": per_yuan,
        "promo_active": bool(promo),
        "promo_until": promo_until,
        "total": len(items),
        "items": items,
    }


# ---- 对外：用量 ----

@public_router.get(
    "/usage",
    summary="查询目前消耗了多少积分与各个模型的调用次数",
    response_description="5 小时 / 24 小时 / 累计三个滚动窗口的金额与次数，以及所选区间按模型汇总的调用量",
)
async def usage(
    window: str = Query(default="24h", pattern="^(5h|24h|total)$", description="不传 from/to 时按这个窗口统计"),
    start: str | None = Query(default=None, alias="from", description="北京时间起点，如 2026-11-01 或 2026-11-01T08:00:00"),
    end: str | None = Query(default=None, alias="to", description="北京时间终点，只给日期时取当天整天"),
) -> dict[str, Any]:
    from .generation_jobs import current_api_key_id

    key_id = current_api_key_id.get()
    store = _store()
    if start or end:
        begin = _bound(start)
        finish = _bound(end, exclusive_end=True)
    else:
        delta = WINDOWS[window]
        begin = (datetime.now(timezone.utc) - delta).isoformat(timespec="milliseconds") if delta else None
        finish = None

    summary, windows, settings = await asyncio.gather(
        asyncio.to_thread(store.usage_report, start=begin, end=finish, key_id=key_id),
        asyncio.to_thread(store.usage_windows, key_id=key_id),
        asyncio.to_thread(store.settings),
    )
    per_yuan = float(settings["credits_per_yuan"])
    for row in summary["by_model"]:
        row["credits"] = round(row["amount_micro"] / MICRO * per_yuan, 4)
    for values in windows.values():
        values["credits"] = round(values["amount_micro"] / MICRO * per_yuan, 4)

    return {
        "scope": "api_key" if key_id else "platform",
        "key_id": key_id,
        "window": window,
        "from": begin,
        "to": finish,
        "credits_per_yuan": per_yuan,
        "windows": windows,
        **summary,
        "credits": round(summary["amount_micro"] / MICRO * per_yuan, 4),
    }
