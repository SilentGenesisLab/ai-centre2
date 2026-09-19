from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TTSProviderName(StrEnum):
    AUTO = "auto"
    VOXCPM2 = "voxcpm2"
    DOUBAO = "doubao"
    ELEVENLABS = "elevenlabs"


class TTSQualityMode(StrEnum):
    STANDARD = "standard"
    STRICT = "strict"


class TTSCloneMode(StrEnum):
    AUTO = "auto"
    CONTROLLABLE = "controllable"
    ULTIMATE = "ultimate"


class TTSEmotionStrategy(StrEnum):
    AUTO = "auto"
    INHERIT = "inherit"
    FORCE = "force"


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
    language: str = Field(
        default="auto",
        min_length=2,
        max_length=16,
        description="语言代码；省略或传 auto 时根据合成文本自动判断",
    )
    voice_profile_id: str = Field(default="default", min_length=1, max_length=128)
    provider: TTSProviderName = TTSProviderName.AUTO
    audio: AudioSpec = Field(default_factory=AudioSpec)
    prosody: ProsodySpec = Field(default_factory=ProsodySpec)
    timing: TimingSpec = Field(default_factory=TimingSpec)
    metadata: dict[str, str] = Field(default_factory=dict)
    seed: int | None = Field(default=None, ge=0, exclude=True)


class TTSCloneSpeechRequest(TTSSpeechRequest):
    reference_audio_path: Path = Field(exclude=True)
    prompt_text: str | None = Field(
        default=None,
        max_length=5000,
        exclude=True,
        description="参考音频文本；缺失或为空时先自动识别参考音频",
    )


class TTSAsyncSpeechRequest(TTSSpeechRequest):
    """Server-side asynchronous synthesis contract for long-form speech."""

    text: str = Field(
        min_length=1,
        max_length=20000,
        description="异步长语音正文，最多20000字符；服务端自动分段并合并。",
    )
    reference_audio_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="可选的公网HTTPS参考音频URL；提供后使用VoxCPM2深度克隆。",
    )
    prompt_text: str | None = Field(
        default=None,
        max_length=5000,
        description="参考音频准确文本；缺失或为空时先自动识别参考音频。",
    )
    emotion: str | None = Field(default=None, max_length=200)
    emotion_enhance: bool = False
    quality_mode: TTSQualityMode = TTSQualityMode.STANDARD
    clone_mode: TTSCloneMode = TTSCloneMode.AUTO
    emotion_strategy: TTSEmotionStrategy = TTSEmotionStrategy.AUTO

    @model_validator(mode="after")
    def require_standard_async_quality(self) -> "TTSAsyncSpeechRequest":
        if self.quality_mode != TTSQualityMode.STANDARD:
            raise ValueError("asynchronous long-form TTS only supports quality_mode=standard")
        return self


class TTSJobRequest(TTSAsyncSpeechRequest):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "text": "这是一条异步语音合成任务。",
                    "language": "zh",
                    "voice_profile_id": "default",
                    "provider": "auto",
                    "idempotency_key": "order-20260802-0001",
                }
            ]
        }
    )

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
