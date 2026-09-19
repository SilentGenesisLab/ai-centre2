from __future__ import annotations

import json
from typing import Any

import redis

from ..config import Settings


class TTSQualityStore:
    def __init__(self, settings: Settings) -> None:
        self._redis = redis.Redis.from_url(
            settings.redis_result_url,
            decode_responses=True,
        )
        self._expires = settings.tts_quality_expires_seconds

    @staticmethod
    def _key(request_id: str) -> str:
        return f"ai-centre2:tts-quality:{request_id}"

    def put(self, request_id: str, payload: dict[str, Any]) -> None:
        self._redis.set(
            self._key(request_id),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ex=self._expires,
        )

    def get(self, request_id: str) -> dict[str, Any] | None:
        raw = self._redis.get(self._key(request_id))
        return json.loads(raw) if raw else None
