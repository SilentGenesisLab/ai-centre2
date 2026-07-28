from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx

from ..base import (
    PermanentTTSProviderError,
    RawSynthesisResult,
    TTSProvider,
)
from ..schemas import TTSSpeechRequest
from .common import raise_for_provider_status, translate_network_error


class DoubaoProvider(TTSProvider):
    name = "doubao"

    def __init__(
        self,
        endpoint: str,
        app_id: str | None,
        access_token: str | None,
        cluster: str,
        timeout_seconds: float,
        enabled: bool,
    ) -> None:
        self.endpoint = endpoint
        self.app_id = app_id
        self.access_token = access_token
        self.cluster = cluster
        self.timeout = httpx.Timeout(timeout_seconds, connect=15)
        self._enabled = bool(enabled and app_id and access_token)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def synthesize(
        self,
        request: TTSSpeechRequest,
        binding: dict[str, Any],
    ) -> RawSynthesisResult:
        voice_type = binding.get("voice_type")
        if not voice_type:
            raise PermanentTTSProviderError(
                "doubao binding requires voice_type"
            )
        if len(request.text.encode("utf-8")) > 1024:
            raise PermanentTTSProviderError(
                "doubao V1 text exceeds the 1024-byte request limit"
            )
        reqid = str(uuid.uuid4())
        payload = {
            "app": {
                "appid": self.app_id,
                "token": "unused",
                "cluster": binding.get("cluster", self.cluster),
            },
            "user": {
                "uid": request.metadata.get("user_id", "ai-centre2"),
            },
            "audio": {
                "voice_type": voice_type,
                "encoding": "mp3",
                "speed_ratio": request.prosody.speed,
                "volume_ratio": request.prosody.volume,
                "pitch_ratio": request.prosody.pitch,
            },
            "request": {
                "reqid": reqid,
                "text": request.text,
                "text_type": "plain",
                "operation": "query",
            },
        }
        headers = {
            "Authorization": f"Bearer;{self.access_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                )
        except Exception as exc:
            raise translate_network_error(exc, self.name) from exc
        raise_for_provider_status(response, self.name)
        body = response.json()
        if body.get("code") != 3000 or not body.get("data"):
            raise PermanentTTSProviderError(
                f"doubao synthesis failed: code={body.get('code')} "
                f"message={body.get('message', '')}"
            )
        try:
            audio = base64.b64decode(body["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise PermanentTTSProviderError(
                "doubao returned invalid base64 audio"
            ) from exc
        duration = body.get("addition", {}).get("duration")
        return RawSynthesisResult(
            audio=audio,
            media_type="audio/mpeg",
            provider=self.name,
            provider_request_id=body.get("reqid", reqid),
            provider_duration_ms=int(duration) if duration else None,
        )

