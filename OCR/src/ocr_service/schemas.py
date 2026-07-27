from __future__ import annotations

from pydantic import BaseModel, Field


class Region(BaseModel):
    name: str = Field(default="full")
    bbox: list[int] | None = Field(default=None, description="[x1, y1, x2, y2]")


class ImageInput(BaseModel):
    image_id: str
    path: str
    time: float | None = None
    regions: list[Region] = Field(default_factory=list)


class BatchRequest(BaseModel):
    job_id: str
    source_lang_hint: str | None = None
    images: list[ImageInput]


class OcrItem(BaseModel):
    bbox: list[int]
    text: str
    score: float
    region: str = "full"


class ImageResult(BaseModel):
    image_id: str
    time: float | None = None
    items: list[OcrItem]


class BatchResponse(BaseModel):
    job_id: str
    engine: str
    model_version: str
    device: str
    elapsed_seconds: float
    results: list[ImageResult]


class SubtitleDetectConfig(BaseModel):
    coarse_interval_seconds: float | None = None
    boundary_window_seconds: float = 0.3
    center_tolerance_top: float = 0.14
    center_tolerance_bottom: float = 0.10
    horizontal_padding_ratio: float = 0.0278
    max_tracks_per_half: int = 1
    min_ocr_score: float = 0.45
    scene_cut_threshold: float = 0.42


class AsrSegment(BaseModel):
    start: float
    end: float
    text: str = ""
    speaker: str | None = None


class SubtitleDetectRequest(BaseModel):
    input_path: str
    output_dir: str | None = None
    source_lang_hint: str | None = None
    mode: str = Field(default="balanced", pattern="^(fast|balanced|accurate)$")
    export_debug_video: bool = True
    asr_segments: list[AsrSegment] = Field(default_factory=list)
    config: SubtitleDetectConfig = Field(default_factory=SubtitleDetectConfig)


class SubtitleDetectMetrics(BaseModel):
    shots: int
    tracks: int
    events: int
    needs_review: int
    elapsed_seconds: float


class SubtitleDetectResponse(BaseModel):
    job_id: str
    status: str
    output_dir: str
    events_json: str
    debug_video: str | None = None
    white_mask_video: str | None = None
    review_html: str | None = None
    qa_json: str
    metrics: SubtitleDetectMetrics
