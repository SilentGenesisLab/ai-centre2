"""把一次生成请求折算成上游积分，用于给 API Key 设「按花钱多少」的日额度。

费率取自 2026-11-05 导出的 LibTV 全年消耗明细（62 积分 = ¥1）：
凡能整除时长的样本，Seedance 2.5 VIP 恒为 110 积分/秒（5s=550、6s=660、
8s=880、12s=1320、15s=1650 全部吻合），Seedance 2.0 Fast VIP 为 22 积分/秒。

只给有实测单价、且请求里带时长（`duration_seconds`）的型号计费；没登记的一律
算 0。宁可漏算也不能乱算：音频类请求里也有 `duration_seconds`（参考音频长度），
按视频费率折算会把一次 TTS 记成 550 分。多算的钱比漏算难解释。
"""

from __future__ import annotations

from typing import Any

CREDITS_PER_SECOND = {
    "seedance-2.5": 110,
    "seedance-2.0-fast": 22,
}

MAX_SECONDS = 30


def rate_for(model: str) -> int:
    code = (model or "").strip().lower()
    for prefix, rate in CREDITS_PER_SECOND.items():
        if code.startswith(prefix):
            return rate
    return 0


def estimate(data: dict[str, Any]) -> int:
    """一次提交预计消耗的积分。不认识的型号返回 0，等于不参与积分额度。"""
    if not isinstance(data, dict):
        return 0
    try:
        seconds = int(data.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return 0
    return min(seconds, MAX_SECONDS) * rate_for(str(data.get("model") or ""))
