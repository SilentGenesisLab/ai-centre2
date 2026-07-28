from __future__ import annotations

from typing import Any

import httpx

from ..base import RawSynthesisResult, TTSProvider
from ..schemas import TTSSpeechRequest
from .common import raise_for_provider_status, translate_network_error


class VoxCPM2Provider(TTSProvider):
    name = "voxcpm2"

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        enabled: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=15)
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def synthesize(
        self,
        request: TTSSpeechRequest,
        binding: dict[str, Any],
    ) -> RawSynthesisResult:
        payload: dict[str, Any] = {
            "text": request.text,
            "cfg_value": binding.get("cfg_value", 2.0),
            "inference_timesteps": binding.get("inference_timesteps", 10),
        }
        endpoint = "tts"
        if binding.get("reference_wav_path") or binding.get("prompt_wav_path"):
            endpoint = "clone_path"
            payload.update(
                {
                    key: binding[key]
                    for key in (
                        "reference_wav_path",
                        "prompt_wav_path",
                        "prompt_text",
                    )
                    if binding.get(key)
                }
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/{endpoint}",
                    json=payload,
                )
        except Exception as exc:
            raise translate_network_error(exc, self.name) from exc
        raise_for_provider_status(response, self.name)
        return RawSynthesisResult(
            audio=response.content,
            media_type=response.headers.get("content-type", "audio/wav"),
            provider=self.name,
            provider_request_id=response.headers.get("x-request-id"),
            metadata={
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"x-elapsed-seconds", "x-audio-duration"}
            },
        )

