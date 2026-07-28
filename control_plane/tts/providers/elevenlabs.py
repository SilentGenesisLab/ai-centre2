from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from ..base import PermanentTTSProviderError, RawSynthesisResult, TTSProvider
from ..schemas import TTSSpeechRequest
from .common import raise_for_provider_status, translate_network_error


class ElevenLabsProvider(TTSProvider):
    name = "elevenlabs"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        default_model_id: str,
        output_format: str,
        timeout_seconds: float,
        enabled: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model_id = default_model_id
        self.output_format = output_format
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
            raise PermanentTTSProviderError(
                "elevenlabs binding requires voice_id"
            )
        language_code = request.language.split("-", 1)[0].lower()
        payload = {
            "text": request.text,
            "model_id": binding.get("model_id", self.default_model_id),
            "language_code": language_code,
            "voice_settings": {
                "speed": request.prosody.speed,
            },
        }
        endpoint = (
            f"{self.base_url}/v1/text-to-speech/{quote(str(voice_id), safe='')}"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    endpoint,
                    params={
                        "output_format": binding.get(
                            "output_format",
                            self.output_format,
                        ),
                        "enable_logging": "false"
                        if binding.get("zero_retention")
                        else "true",
                    },
                    headers={
                        "xi-api-key": self.api_key or "",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            raise translate_network_error(exc, self.name) from exc
        raise_for_provider_status(response, self.name)
        return RawSynthesisResult(
            audio=response.content,
            media_type=response.headers.get("content-type", "audio/mpeg"),
            provider=self.name,
            provider_request_id=(
                response.headers.get("request-id")
                or response.headers.get("x-request-id")
            ),
            metadata={
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"character-cost", "x-trace-id"}
            },
        )

