"""把一次生成请求折算成人民币，用于写账和给 API Key 设限额。

口径：**上游实测成本**。LibTV 的全年消耗明细（2026-11-05 导出）里，
积分能整除时长的样本给出稳定的单价 —— Seedance 2.5 VIP 恒为 110 积分/秒
（4s=440、5s=550、6s=660、8s=880、12s=1320、15s=1650 全部吻合），
Seedance 2.0 Fast VIP 为 22 积分/秒，Seedance 2.0 VIP 为 49 积分/秒。
账号充值实测 62 积分 = ¥1，所以每单位金额 = 积分单价 ÷ 62。

其他渠道（grsai / dashscope / jmapi / mxapi）没有可对账的导出，费率是**估算**，
`source` 字段标成 `estimated`，管理台会把它们标「待校准」；用户可随时改。

金额一律用**微元**（1e-6 元）存整数，避免浮点累加误差。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

MICRO = 1_000_000
CREDITS_PER_YUAN = Decimal(62)

RateKey = tuple[str, str, str]


@dataclass(frozen=True)
class Quote:
    unit: str
    quantity: float
    amount_micro: int
    source: str


def credits_to_micro(credits: Decimal | int | float, per_yuan: Decimal | int | float = CREDITS_PER_YUAN) -> int:
    """积分单价 → 每单位微元。四舍五入到 1e-6 元。"""
    value = Decimal(str(credits)) / Decimal(str(per_yuan)) * MICRO
    return int(value.quantize(Decimal("1")))


def from_yuan(value: str | int | float | None) -> int | None:
    """管理台输入的元 → 微元。空值表示不限。"""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid amount: {value!r}") from exc
    if amount < 0:
        raise ValueError("amount must not be negative")
    return int(amount * MICRO)


def to_yuan(amount_micro: int | None) -> float | None:
    if amount_micro is None:
        return None
    return round(amount_micro / MICRO, 4)


# (service, model, variant, unit, 积分/单位 或 None=直接用微元, source)
_RATE_SEED_CREDITS: list[tuple[str, str, str, str, Decimal | None, str]] = [
    ("video_generation", "seedance-2.5", "*", "second", Decimal(110), "measured"),
    ("video_generation", "seedance-2.0", "*", "second", Decimal(49), "measured"),
    ("video_generation", "seedance-2.0-fast", "*", "second", Decimal(22), "measured"),
    ("video_generation", "seedance-2.0-mini", "*", "second", Decimal(32), "measured"),
    ("video_generation", "minimax-h3-rh-enhanced", "*", "second", Decimal(14), "measured"),
    ("audio_generation", "suno-v6", "*", "track", Decimal(186), "estimated"),
    ("audio_generation", "suno-sound", "*", "track", Decimal(62), "estimated"),
]

# 没有实测积分、直接按元估的（(service, model, variant, unit, 元/单位, source)）
_RATE_SEED_YUAN: list[tuple[str, str, str, str, str, str]] = [
    ("video_generation", "kling-*", "*", "second", "0.23", "estimated"),
    ("video_generation", "hailuo-*", "*", "second", "0.23", "estimated"),
    ("video_generation", "wan-*", "*", "second", "0.23", "estimated"),
    ("video_generation", "vidu-*", "*", "second", "0.23", "estimated"),
    ("video_generation", "pixverse-*", "*", "second", "0.23", "estimated"),
    ("video_generation", "omnihuman-*", "*", "second", "0.23", "estimated"),
    ("video_generation", "*", "*", "second", "0.23", "estimated"),
    ("image_generation", "gpt-image-2.5", "1K", "image", "0.30", "estimated"),
    ("image_generation", "gpt-image-2.5", "2K", "image", "0.50", "estimated"),
    ("image_generation", "gpt-image-2.5", "4K", "image", "1.20", "estimated"),
    ("image_generation", "gpt-image-2.5", "*", "image", "0.30", "estimated"),
    ("image_generation", "nano-banana-2", "*", "image", "0.35", "estimated"),
    ("image_generation", "nanobanana-2.1", "*", "image", "0.35", "estimated"),
    ("image_generation", "gpt-image-2", "*", "image", "0.30", "estimated"),
    ("image_generation", "gpt-image-2-4k-upscale", "*", "image", "1.50", "estimated"),
    ("image_generation", "moss-image", "*", "image", "0", "free"),
    ("image_generation", "*", "*", "image", "0.35", "estimated"),
    ("audio_generation", "*", "*", "track", "2.00", "estimated"),
    ("text_generation", "*", "*", "call", "0.02", "estimated"),
    ("decision_generation", "*", "*", "call", "0.02", "estimated"),
    ("tts", "voxcpm2*", "*", "1000_chars", "0", "free"),
    ("tts", "*", "*", "1000_chars", "0.10", "estimated"),
    ("asr", "*", "*", "call", "0.02", "estimated"),
    ("ocr", "*", "*", "image", "0.01", "estimated"),
    ("upscale", "*", "*", "call", "0.50", "estimated"),
    ("lipsync", "*", "*", "call", "0.50", "estimated"),
    ("scene", "*", "*", "call", "0.50", "estimated"),
    ("review", "*", "*", "call", "0.50", "estimated"),
    ("watermark", "*", "*", "call", "0.50", "estimated"),
    ("depth", "*", "*", "call", "0.50", "estimated"),
    ("separation", "*", "*", "call", "0.50", "estimated"),
    ("color_grade", "*", "*", "call", "0.50", "estimated"),
    ("face_mosaic", "*", "*", "call", "0.50", "estimated"),
]

def seed_rates() -> list[tuple[str, str, str, str, int, str]]:
    """初始费率表。返回 (service, model, variant, unit, amount_micro, source)。"""
    rows: list[tuple[str, str, str, str, int, str]] = []
    for service, model, variant, unit, credits, source in _RATE_SEED_CREDITS:
        rows.append((service, model, variant, unit, credits_to_micro(credits), source))
    for service, model, variant, unit, yuan, source in _RATE_SEED_YUAN:
        rows.append((service, model, variant, unit, int(Decimal(yuan) * MICRO), source))
    return rows


def _specificity(pattern: str, value: str) -> int:
    """0 = 通配、1 = 前缀通配、2 = 完全相等。不匹配返回 -1。"""
    if pattern == value:
        return 2
    if pattern == "*":
        return 0
    if pattern.endswith("*") and value.startswith(pattern[:-1]):
        return 1
    return -1


def lookup(rates: Iterable[Mapping[str, Any]], service: str, model: str, variant: str) -> tuple[int, str, str] | None:
    """最具体的命中优先：型号越精确、变体越精确越好，同分取后写入的那条。

    rates 的每一项至少要有 service/model/variant/amount_micro/source/unit/enabled。
    返回 (amount_micro, unit, source)，找不到返回 None。
    """
    best: tuple[int, Mapping[str, Any]] | None = None
    for index, row in enumerate(rates):
        if row["service"] != service or not row["enabled"]:
            continue
        model_score = _specificity(str(row["model"]), model)
        variant_score = _specificity(str(row["variant"]), variant)
        if model_score < 0 or variant_score < 0:
            continue
        score = model_score * 3 + variant_score
        if best is None or score >= best[0]:
            best = (score, row)
    if best is None:
        return None
    row = best[1]
    return int(row["amount_micro"]), str(row["unit"]), str(row["source"])


def variant_for(service: str, data: Mapping[str, Any]) -> str:
    if service == "video_generation":
        value = str(data.get("resolution") or "").strip().lower()
        return value or "720p"
    if service == "image_generation":
        value = str(data.get("image_size") or "").strip().upper()
        return value or "1K"
    return "*"


def _count(data: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return float(len(value) or 1)
        if isinstance(value, int) and value > 0:
            return float(value)
    return 1.0


def quantity_for(service: str, data: Mapping[str, Any]) -> tuple[str, float]:
    if service == "video_generation":
        try:
            seconds = int(data.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        return "second", float(min(max(seconds, 1), 30)) if seconds > 0 else 5.0
    if service == "image_generation":
        return "image", _count(data, "n", "count")
    if service == "tts":
        text = str(data.get("text") or "")
        return "1000_chars", max(len(text), 1) / 1000
    if service == "ocr":
        return "image", _count(data, "image_urls", "files", "images")
    if service == "audio_generation":
        return "track", 1.0
    return "call", 1.0


def estimate_request(
    rates: Iterable[Mapping[str, Any]],
    service: str,
    data: Mapping[str, Any],
    model: str = "",
) -> Quote:
    """一次提交预计花多少钱。查不到费率就记 0 元 —— 仍然落账，只是金额未知。"""
    model = str(model or data.get("model") or "")
    unit, quantity = quantity_for(service, data)
    variant = variant_for(service, data)
    found = lookup(rates, service, model, variant)
    if found is None:
        return Quote(unit=unit, quantity=quantity, amount_micro=0, source="unknown")
    amount, rate_unit, source = found
    return Quote(
        unit=rate_unit,
        quantity=quantity,
        amount_micro=int(round(amount * quantity)),
        source=source,
    )
