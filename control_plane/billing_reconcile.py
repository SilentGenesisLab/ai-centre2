"""把卡在 held 的预占账按作业真实终态收尾。

预占额度只有在结算口被调用时才退回。H3 这类只靠**客户端轮询**结算的端点，
提交方一旦不再查询，作业早就结束了、账上却一直挂着 held，永久占着 5 小时 /
24 小时 / 累计三个窗口——额度被自己吃掉了，明细页却看不出任何异常。
"""

from __future__ import annotations

from collections.abc import Callable

# 视频作业的运行时长是分钟级，留 6 小时是对「上游排队很久」的余量，
# 而不是对作业时长的估计。
STALE_AFTER_HOURS = 6.0

TERMINAL = {"succeeded", "failed", "cancelled"}


def _status_of(job_id: str) -> str | None:
    """作业在哪张表里都试一遍；查不到就返回 None（可能不是作业型的账）。"""
    from .api import get_capability_store, get_h3_store

    for store in (get_capability_store(), get_h3_store()):
        try:
            row = store.job(job_id, include_request=False)
        except Exception:
            continue
        if row:
            return str(row.get("status") or "") or None
    return None


def reconcile_held(
    older_than_hours: float = STALE_AFTER_HOURS,
    status_of: Callable[[str], str | None] = _status_of,
) -> dict[str, int]:
    """只处理两种确定的情况：作业已终结的按终态落定，根本没建出作业的按失败退回。

    还在跑的、以及有 job_id 但两张表都查不到的，一律不动——宁可继续占着额度，
    也不能把一笔真花掉的钱退回去。
    """
    from .api import get_api_key_store

    keys = get_api_key_store()
    settled = refunded = pending = 0
    for row in keys.held_charges(older_than_hours):
        job_id = row.get("job_id")
        status = status_of(job_id) if job_id else None
        if status in TERMINAL:
            keys.settle_job(str(job_id), status)
            settled += 1
        elif job_id is None:
            keys.settle(row["request_id"], "failed")
            refunded += 1
        else:
            pending += 1
    return {"settled": settled, "refunded": refunded, "pending": pending}
