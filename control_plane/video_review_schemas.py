from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReferenceAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    kind: Literal["image", "video"]
    label: str | None = Field(default=None, max_length=120)


class VideoReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_url: str
    language: str = Field(default="auto", min_length=1, max_length=16)
    analysis_profile: Literal["advertising", "short_drama", "product_demo"] = "advertising"
    include_audio: bool = True
    include_transcript: bool = True
    continuity_check: bool = False
    reference_assets: list[ReferenceAsset] = Field(default_factory=list, max_length=8)
    external_ref: str | None = Field(default=None, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("video_url")
    @classmethod
    def validate_video_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("video_url is required")
        return value

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return value.strip().lower() or "auto"


class VideoReviewAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str
    report_url: str

