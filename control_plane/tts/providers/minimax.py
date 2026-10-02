from __future__ import annotations

import math
from typing import Any

import httpx

from ..base import (
    PermanentTTSProviderError,
    RawSynthesisResult,
    TransientTTSProviderError,
    TTSProvider,
)
from ..schemas import TTSSpeechRequest
from .common import raise_for_provider_status, translate_network_error

# MiniMax 把业务错误放在 HTTP 200 响应的 base_resp 里，只看 HTTP 状态码会把
# 「鉴权失败」当成成功（实测 1004 就是 HTTP 200）。这份可重试集合取自官方错误码表
# https://platform.minimax.io/docs/api-reference/errorcode
_RETRYABLE_STATUS_CODES = frozenset({1000, 1001, 1002, 1024, 1033, 1039, 2045})

# MiniMax 只接受这几档采样率；调用方通常要 48000，必须向下取一档。
_SUPPORTED_SAMPLE_RATES = (8000, 16000, 22050, 24000, 32000, 44100)

_LANGUAGE_BOOST = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ru": "Russian",
}


def _sample_rate(requested: int) -> int:
    available = [rate for rate in _SUPPORTED_SAMPLE_RATES if rate <= requested]
    return available[-1] if available else _SUPPORTED_SAMPLE_RATES[0]


class MiniMaxProvider(TTSProvider):
    name = "minimax"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        default_model: str,
        timeout_seconds: float,
        enabled: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.timeout = httpx.Timeout(timeout_seconds, connect=15)
        self._enabled = bool(enabled and api_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def synthesize(
        self,
        request: TTSSpeechRequest,
        binding: dict[str, Any],
    ) -> RawSynthesisResult:
        voice_id = binding.get("voice_id")
        if not voice_id:
            raise PermanentTTSProviderError("minimax binding requires voice_id")
        payload: dict[str, Any] = {
            "model": binding.get("model", self.default_model),
            "text": request.text,
            "stream": False,
            "language_boost": binding.get(
                "language_boost",
                _LANGUAGE_BOOST.get(request.language.split("-", 1)[0].lower(), "auto"),
            ),
            "voice_setting": {
                "voice_id": voice_id,
                "speed": request.prosody.speed,
                "vol": request.prosody.volume,
                # ProsodySpec.pitch 是倍率（1.0 为原调），MiniMax 要半音数。
                "pitch": max(-12, min(12, round(12 * math.log2(request.prosody.pitch)))),
            },
            "audio_setting": {
                "sample_rate": _sample_rate(request.audio.sample_rate),
                "bitrate": 128000,
                "format": "mp3",
                "channel": request.audio.channels,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/v1/t2a_v2",
                    headers={
                        "Authorization": f"Bearer {self.api_key or ''}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            raise translate_network_error(exc, self.name) from exc
        raise_for_provider_status(response, self.name)

        body = response.json()
        base_response = body.get("base_resp") or {}
        status_code = base_response.get("status_code")
        if status_code != 0:
            detail = (
                f"minimax returned status_code={status_code}: "
                f"{str(base_response.get('status_msg') or '').strip()[:160]}"
            )
            if status_code in _RETRYABLE_STATUS_CODES:
                raise TransientTTSProviderError(detail)
            raise PermanentTTSProviderError(detail)

        audio_hex = (body.get("data") or {}).get("audio")
        if not audio_hex:
            raise PermanentTTSProviderError("minimax returned an empty audio field")
        try:
            audio = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise PermanentTTSProviderError(
                "minimax returned audio that is not valid hex"
            ) from exc

        extra = body.get("extra_info") or {}
        duration = extra.get("audio_length")
        return RawSynthesisResult(
            audio=audio,
            media_type="audio/mpeg",
            provider=self.name,
            provider_request_id=response.headers.get("x-request-id"),
            provider_duration_ms=int(duration) if duration else None,
            metadata={"usage_characters": extra.get("usage_characters")},
        )
