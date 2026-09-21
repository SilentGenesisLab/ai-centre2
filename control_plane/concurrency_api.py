"""控制台的「并发与调度」后端：读当前两级并发、热改运行期值。

两个边界写在这里，因为它们是这个接口存在的理由：

- **上限**（systemd 单元里的池大小）只读，从单元实时读回来。目标值超过上限时如实标注
  不生效，而不是静默接受一个不会生效的数。
- **下限**（config.py 里的默认值）不可改，只能被覆盖。`POST /reset` 就是回到它。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .concurrency import (
    MAX_LIMIT,
    MIN_LIMIT,
    all_module_states,
    get_runtime_store,
    registered_keys,
)
from .observability_api import verify_internal_token

router = APIRouter(prefix="/internal/admin/concurrency", include_in_schema=False)


class ConcurrencyConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, int] = Field(default_factory=dict)


class ConcurrencyResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] = Field(default_factory=list)


def _validate(values: dict[str, int]) -> dict[str, int]:
    unknown = sorted(set(values) - registered_keys())
    if unknown:
        # 白名单之外一律拒绝：静默丢弃会让「我改了但没生效」变成又一个查不出来的问题。
        raise HTTPException(status_code=422, detail=f"未登记的并发参数：{', '.join(unknown)}")
    out_of_range = sorted(key for key, value in values.items() if not MIN_LIMIT <= value <= MAX_LIMIT)
    if out_of_range:
        raise HTTPException(
            status_code=422,
            detail=f"并发值必须在 {MIN_LIMIT}..{MAX_LIMIT} 之间：{', '.join(out_of_range)}",
        )
    return values


@router.get("", dependencies=[Depends(verify_internal_token)])
async def states() -> dict[str, Any]:
    return all_module_states()


@router.put("/config", dependencies=[Depends(verify_internal_token)])
async def update_config(request: ConcurrencyConfigRequest) -> dict[str, Any]:
    get_runtime_store().update(_validate(request.values), registered_keys())
    return all_module_states()


@router.post("/reset", dependencies=[Depends(verify_internal_token)])
async def reset_config(request: ConcurrencyResetRequest) -> dict[str, Any]:
    keys = request.keys or sorted(registered_keys())
    unknown = sorted(set(keys) - registered_keys())
    if unknown:
        raise HTTPException(status_code=422, detail=f"未登记的并发参数：{', '.join(unknown)}")
    get_runtime_store().reset(keys)
    return all_module_states()
