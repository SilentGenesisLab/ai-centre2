"""非作业型生成端点的记账钩子。

用法：路由上挂 `dependencies=[Depends(billable("tts"))]`，端点体里不用任何改动。
作业型端点（真的会返回 job_id 让人轮询的）不要挂这个——它们在创建作业的地方
自己 `hold`，由轮询口 `settle`；挂上来会立刻把还没跑完的单结算成成功。

账单口径与费率见 credit_pricing；限额检查见 ApiKeyStore.hold。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import HTTPException, Request, status

from .api_keys import ApiKeyStore, CreditQuotaExceeded
from .credit_pricing import estimate_request
from .generation_jobs import current_api_key_id


@dataclass
class BillingContext:
    request_id: str
    charge_id: str
    service: str
    key_id: str | None
    amount_micro: int
    deferred: bool = False


_current_charge: ContextVar[BillingContext | None] = ContextVar("current_billing_charge", default=None)


def _store() -> ApiKeyStore:
    # 延迟导入：api.py 里这些 getter 都建在模块顶层，直接 import 会绕成循环。
    from .api import get_api_key_store

    return get_api_key_store()


async def _payload(request: Request) -> dict[str, Any]:
    """按需读 body 用于估价。multipart 一律不读——那是上传口，读了要落盘。"""
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        return {}
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def _hold(service: str, request: Request, model_hint: str | None) -> tuple[ApiKeyStore, BillingContext]:
    store = _store()
    data = await _payload(request)
    rates = await asyncio.to_thread(store.rates_cached)
    model = model_hint or str(data.get("model") or "")
    quote = estimate_request(rates, service, data, model)
    request_id = uuid4().hex
    key_id = current_api_key_id.get()
    try:
        held = await asyncio.to_thread(
            store.hold, key_id, request_id, service, model or None, data.get("channel"), quote
        )
    except CreditQuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"billing limit [{exc.reason}]: {exc}",
        ) from exc
    ctx = BillingContext(
        request_id=request_id,
        charge_id=str(held["charge_id"]),
        service=service,
        key_id=key_id,
        amount_micro=quote.amount_micro,
    )
    return store, ctx


def billable(service: str, model: str | None = None):
    """`Depends(billable(service))`：进来先预占额度，出去按结果结算。

    `model` 用在请求体里没有 model 字段的端点（如 MiniMax H3），否则估价会落到
    服务级通配价上，算出来的钱和实际扣的不一样。

    必须是 async——同步依赖会被 FastAPI 丢进线程池，在那里 set 的 ContextVar
    传不回端点（tests/test_credit_quota_plumbing.py 钉着这条）。
    """

    async def dependency(request: Request) -> AsyncIterator[BillingContext]:
        store, ctx = await _hold(service, request, model)
        token = _current_charge.set(ctx)
        try:
            yield ctx
        except BaseException:
            await asyncio.to_thread(store.settle, ctx.request_id, "failed")
            raise
        else:
            if not ctx.deferred:
                await asyncio.to_thread(store.settle, ctx.request_id, "succeeded")
        finally:
            _current_charge.reset(token)

    return dependency


def current_charge() -> BillingContext | None:
    return _current_charge.get()


def link_current(job_id: str) -> str | None:
    """作业建好后把 charge 与 job 绑上，并把结算交给轮询口。返回 job_id 便于链式写。"""
    ctx = _current_charge.get()
    if ctx is None or not job_id:
        return job_id
    ctx.deferred = True
    _store().link(ctx.charge_id, job_id)
    return job_id
