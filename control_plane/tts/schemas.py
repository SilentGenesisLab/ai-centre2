from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class TTSProviderName(StrEnum):
    AUTO = "auto"
    VOXCPM2 = "voxcpm2"
    DOUBAO = "doubao"
    ELEVENLABS = "elevenlabs"


class AudioSpec(BaseModel):
    format: str = "wav"
    sample_rate: int = Field(default=48000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)

    @field_validator("format")
    @classmethod
    def require_wav(cls, value: str) -> str:
        if value.lower() != "wav":
            raise ValueError("the canonical V2 output format is wav")
        return "wav"


class ProsodySpec(BaseModel):
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.1, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=2.0)


class TimingSpec(BaseModel):
    target_duration_ms: int | None = Field(default=None, ge=100, le=600000)
    tolerance_ms: int = Field(default=200, ge=0, le=10000)


class TTSSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    language: str = Field(min_length=2, max_length=16)
    voice_profile_id: str = Field(default="default", min_length=1, max_length=128)
    provider: TTSProviderName = TTSProviderName.AUTO
    audio: AudioSpec = Field(default_factory=AudioSpec)
    prosody: ProsodySpec = Field(default_factory=ProsodySpec)
    timing: TimingSpec = Field(default_factory=TimingSpec)
    metadata: dict[str, str] = Field(default_factory=dict)


class TTSJobRequest(TTSSpeechRequest):
    idempotency_key: str = Field(min_length=8, max_length=256)


class VoiceProfile(BaseModel):
    voice_profile_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    languages: list[str] = Field(default_factory=list)
    bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fallback_order: list[TTSProviderName] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_providers(self) -> "VoiceProfile":
        allowed = {
            TTSProviderName.VOXCPM2.value,
            TTSProviderName.DOUBAO.value,
            TTSProviderName.ELEVENLABS.value,
        }
        unknown = set(self.bindings) - allowed
        if unknown:
            raise ValueError(f"unsupported voice binding providers: {sorted(unknown)}")
        if TTSProviderName.AUTO in self.fallback_order:
            raise ValueError("fallback_order cannot contain auto")
        return self


class TTSJobAccepted(BaseModel):
    job_id: str
    status: str
    duplicate: bool = False


class TTSJobStatus(BaseModel):
    job_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None

