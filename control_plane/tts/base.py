from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .schemas import TTSSpeechRequest


class TTSProviderError(RuntimeError):
    pass


class TransientTTSProviderError(TTSProviderError):
    pass


class PermanentTTSProviderError(TTSProviderError):
    pass


@dataclass(frozen=True)
class RawSynthesisResult:
    audio: bytes
    media_type: str
    provider: str
    provider_request_id: str | None = None
    provider_duration_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes
    provider: str
    audio_duration_ms: int
    provider_request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TTSProvider(ABC):
    name: str

    @property
    @abstractmethod
    def enabled(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def synthesize(
        self,
        request: TTSSpeechRequest,
        binding: dict[str, Any],
    ) -> RawSynthesisResult:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
        }

