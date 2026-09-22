from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import random
import secrets
import shutil
import sqlite3
import tempfile
import time
import unicodedata
import wave
from array import array
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pypinyin import Style, lazy_pinyin
from pykakasi import kakasi
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import get_settings
from .gpu_control import GpuController
from .lipsync import LipSyncClient, LipSyncUpstreamError
from .media_fetch import (
    AUDIO_MEDIA,
    ASR_MEDIA,
    IMAGE_MEDIA,
    VIDEO_MEDIA,
    MediaFetchError,
    download_public_media_async,
    sniff_media_suffix,
    validate_public_https_url,
)
from .scene_jobs import SceneJobClient, SceneJobNotFound
from .video_review_jobs import VideoReviewJobClient, VideoReviewJobNotFound
from .video_review_schemas import VideoReviewRequest
from .watermark_jobs import WatermarkJobClient, WatermarkJobNotFound
from .depth_jobs import DepthJobClient, DepthJobNotFound
from .audio_separation_jobs import (
    AudioSeparationJobClient,
    AudioSeparationJobNotFound,
)
from .color_grade_jobs import ColorGradeJobClient, ColorGradeJobNotFound
from .video_upscale_jobs import VideoUpscaleJobClient, VideoUpscaleJobNotFound
from .h3_jobs import H3JobClient, H3JobNotFound
from .h3_scheduler import H3Scheduler, probe_worker
from .h3_pool import H3PoolManager
from .h3_store import H3Store, H3WorkerNotFound
from .h3_workflow import resolve_dimensions
from .observability import ObservabilityMiddleware
from .observability_api import get_observability_store, router as observability_router
from .health_monitor_api import get_health_monitor, router as health_monitor_router
from .concurrency_api import router as concurrency_router
from .api_keys import ApiKeyStore
from .ai_capabilities import CapabilityStore, CapabilityNotFound
from .generation_jobs import GenerationJobClient
from .generation_tasks import _compatible as generation_request_compatible
from .generation_tasks import probe_channel
from .oss_storage import OssStorage
from .tts.base import (
    PermanentTTSProviderError,
    TransientTTSProviderError,
    TTSProviderError,
)
from .tts.jobs import TTSJobClient, TTSJobNotFound, TTSJobNotReady
from .tts.enhanced import (
    FADE_MS,
    PARAGRAPH_SILENCE_MS,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    SENTENCE_SILENCE_MS,
    apply_prosody_wav,
    build_style_instruction,
    combine_wav_segments,
    enhance_emotion,
    model_text,
    normalize_wav_silence,
    pcm_silence,
    process_pcm_stream,
    split_tts_text,
    wav_duration_seconds,
    wav_silence_metrics,
)
from .tts.quality_store import TTSQualityStore
from .tts.reference import (
    REFERENCE_CHANNELS,
    REFERENCE_SAMPLE_RATE,
    REFERENCE_TAIL_SILENCE_SECONDS,
    append_wav_silence,
    infer_text_language,
    languages_differ,
    normalize_language,
    reference_window_ranges,
    signal_to_noise_db,
    slice_wav,
    speech_coverage,
    unclipped_score,
    wav_speech_segments,
    wav_duration_seconds as reference_wav_duration_seconds,
    window_score,
)
from .tts.runtime import get_tts_service
from .tts.schemas import (
    AudioSpec,
    ProsodySpec,
    TTSCloneSpeechRequest,
    TTSCloneMode,
    TTSEmotionStrategy,
    TTSJobAccepted,
    TTSJobRequest,
    TTSJobStatus,
    TTSQualityMode,
    TTSSpeechRequest,
    TTSProviderName,
    TimingSpec,
    VoiceProfile,
)
from .tts.voices import VoiceProfileNotFound
from .upstreams import AudioUpstreams
from .tts.audio import normalize_to_wav, trim_wav_end


app = FastAPI(
    title="AI Centre 2 企业级 AI 中台接口",
    description="面向第三方系统的 URL 素材接口。除健康检查外，所有接口均需 Bearer Token。",
    version="2.0.0",
    openapi_tags=[
        {"name": "系统状态", "description": "中台及上游服务健康状态。"},
        {"name": "文件上传", "description": "把本地素材上传到中台自有 OSS 暂存区，换取公网 HTTPS 直链。"},
        {"name": "唇形驱动", "description": "MuseTalk 唇形驱动与可选 GFPGAN 人脸修复。"},
        {"name": "语音识别", "description": "音频或视频的中文语音识别与时间分段。"},
        {"name": "语音合成", "description": "普通 TTS、VoxCPM2 深度语音克隆及异步任务。"},
        {"name": "OCR 文字识别", "description": "公网 HTTPS 图片批量文字识别。"},
        {"name": "人脸处理", "description": "视频人脸处理任务提交、查询与取消。"},
        {"name": "视频切片", "description": "使用 SceneDetect 检测场景并输出 OSS 视频切片。"},
        {"name": "AI 视频拉片", "description": "无参考视频拉片、拆镜、声音与问题证据分析。"},
        {"name": "水印处理", "description": "视频水印脱敏的同步与异步处理。"},
    ],
    servers=[
        {
            "url": "https://aicentre2.sligenai.cn:8443",
            "description": "生产 HTTPS 网关",
        },
        {
            "url": "http://127.0.0.1:8320",
            "description": "服务器本机调试地址",
        },
    ],
)
app.add_middleware(ObservabilityMiddleware, store_factory=get_observability_store)
app.include_router(observability_router)
app.include_router(health_monitor_router)
app.include_router(concurrency_router)
app.openapi_tags.append(
    {
        "name": "视频深度推理",
        "description": "使用 Video Depth Anything Small 生成单目灰度深度视频。",
    }
)
app.openapi_tags.append({"name":"通用视频生成","description":"按模型和渠道提交原子视频生成任务。"})
app.openapi_tags.append({"name":"图像生成","description":"通过已注册渠道提交原子图像生成任务。"})
app.openapi_tags.append(
    {
        "name": "音频分离",
        "description": "使用Bandit v2将音视频分离为对白、音乐、音效和背景轨。",
    }
)
app.openapi_tags.append(
    {
        "name": "视频调色",
        "description": "使用 .cube 3D LUT 异步处理视频并返回成片 URL。",
    }
)
app.openapi_tags.append(
    {
        "name": "视频超分",
        "description": "FlashVSR V2、FlashVSR与SeedVR2智能渠道视频超分。",
    }
)
app.openapi_tags.append(
    {
        "name": "MiniMax H3视频生成",
        "description": "纯文本或参考素材驱动的MiniMax H3异步视频生成与多机调度。",
    }
)

ASR_UPLOAD_SUFFIXES = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
}
TTS_REFERENCE_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_OCR_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OCR_IMAGES = 20
REFERENCE_EMOTION_CACHE_TTL_SECONDS = 3600.0
REFERENCE_EMOTION_CACHE_MAX_ENTRIES = 128
_REFERENCE_EMOTION_CACHE: dict[str, tuple[float, str | None]] = {}
_REFERENCE_WINDOW_CACHE: dict[str, tuple[float, float, float]] = {}
_SPEAKER_SIMILARITY_CACHE: dict[str, tuple[float, float]] = {}
SPEAKER_SIMILARITY_CACHE_MAX_ENTRIES = 1024


class ASRUrlRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "file_url": "https://storage.example.com/audio/demo-10s.mp3",
                    "language": "auto",
                    "beam_size": 5,
                }
            ]
        },
    )

    file_url: str = Field(min_length=1, max_length=4096)
    language: str | None = Field(
        default="auto",
        min_length=2,
        max_length=16,
        description="语言代码；省略或传 auto 时自动识别",
    )
    beam_size: int = Field(default=5, ge=1, le=10)


class LipSyncUrlRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "video_url": "https://storage.example.com/video/input.mp4",
                    "audio_url": "https://storage.example.com/audio/speech-10s.mp3",
                    "face_restore": True,
                }
            ]
        },
    )

    video_url: str = Field(min_length=1, max_length=4096)
    audio_url: str = Field(min_length=1, max_length=4096)
    face_restore: bool = False


class TTSPublicSpeechRequest(TTSSpeechRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "text": "您好，这是 AI Centre 2 的语音合成示例。",
                    "voice_profile_id": "default",
                    "provider": "auto",
                },
                {
                    "text": "这段文字将使用参考音频进行深度语音克隆。",
                    "voice_profile_id": "default",
                    "reference_audio_url": "https://storage.example.com/audio/reference-10s.mp3",
                    "prompt_text": "参考音频中准确说出的文字",
                    "emotion": "真诚、温暖、有感染力",
                    "emotion_enhance": False,
                    "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0},
                    "quality_mode": "standard",
                },
            ]
        },
    )

    reference_audio_url: str | None = Field(default=None, min_length=1, max_length=4096)
    prompt_text: str | None = Field(
        default=None,
        max_length=5000,
        description=(
            "参考音频准确文本；非空时直接使用，缺失或为空时先自动 ASR；"
            "生成结果使用内容 CER 与说话人相似度双门禁"
        ),
    )
    emotion: str | None = Field(
        default=None,
        max_length=200,
        description="可选的自然语言情绪、节奏、停顿和重音要求",
    )
    emotion_enhance: bool = Field(
        default=False,
        description="使用配置的豆包大模型增强情绪表达指令；失败时自动降级",
    )
    quality_mode: TTSQualityMode = Field(
        default=TTSQualityMode.STANDARD,
        description=(
            "standard默认单次生成并异步审计；需要同步门禁的克隆会按需重试，"
            "跨语言最多六个候选并返回最佳结果"
        ),
    )
    clone_mode: TTSCloneMode = Field(
        default=TTSCloneMode.AUTO,
        description="克隆模式：auto 自动路由、controllable 隔离参考、ultimate 延续克隆",
    )
    emotion_strategy: TTSEmotionStrategy = Field(
        default=TTSEmotionStrategy.AUTO,
        description="情绪策略：auto 自动判断、inherit 继承参考、force 强制目标情绪",
    )


@dataclass(frozen=True)
class CloneContext:
    original_reference_path: Path | None = None
    model_reference_path: Path | None = None
    isolated_reference_path: Path | None = None
    audit_prompt_text: str | None = None
    model_prompt_text: str | None = None
    reference_language: str | None = None
    target_language: str | None = None
    effective_mode: TTSCloneMode | None = None
    fallback: str = "none"
    cross_language: bool = False
    emotion_strategy: TTSEmotionStrategy = TTSEmotionStrategy.INHERIT
    style: str = ""
    requested_emotion: str | None = None
    enhancement_status: str = "disabled"
    reference_window_start: float | None = None
    reference_window_duration: float | None = None

    @property
    def candidate_count(self) -> int:
        return 3 if self.cross_language else 1


class OCRRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="full", min_length=1, max_length=128)
    bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)


class OCRUrlImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=4096)
    time: float | None = None
    regions: list[OCRRegion] = Field(default_factory=list, max_length=32)


class OCRUrlBatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "job_id": "order-20260802-001",
                    "source_lang_hint": "zh",
                    "images": [
                        {
                            "image_id": "front",
                            "url": "https://storage.example.com/images/front.png",
                            "regions": [],
                        }
                    ],
                }
            ]
        },
    )

    job_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_lang_hint: str | None = Field(default=None, min_length=2, max_length=16)
    images: list[OCRUrlImage] = Field(min_length=1, max_length=MAX_OCR_IMAGES)


class FaceUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_uri": "https://storage.example.com/video/source.mp4",
                    "filename": "face_mosaic.mp4",
                    "external_ref": "order-20260802-001",
                    "metadata": {},
                }
            ]
        },
    )

    source_uri: str = Field(min_length=1, max_length=4096)
    filename: str = Field(default="face_mosaic.mp4", min_length=1, max_length=256)
    external_ref: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    campaign_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

class SceneUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_uri": "https://storage.example.com/video/source.mp4",
                    "filename": "scene.mp4",
                    "threshold": 27.0,
                    "min_scene_len": 15,
                    "external_ref": "order-20260803-001",
                    "metadata": {},
                }
            ]
        },
    )

    source_uri: str = Field(min_length=1, max_length=4096)
    filename: str = Field(
        default="scene.mp4",
        min_length=1,
        max_length=256,
        pattern=r"^[^/\\]+$",
    )
    threshold: float = Field(default=27.0, ge=1.0, le=255.0)
    min_scene_len: int = Field(default=15, ge=1, le=1000)
    external_ref: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    campaign_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoReviewUrlJobRequest(VideoReviewRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{
                "video_url": "https://storage.example.com/video/ad.mp4",
                "language": "auto",
                "analysis_profile": "advertising",
                "include_audio": True,
                "include_transcript": True,
                "continuity_check": False,
                "reference_assets": [],
                "external_ref": "review-001",
                "metadata": {},
            }]
        },
    )

class WatermarkUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_uri": "https://storage.example.com/video/source.mp4",
                    "filename": "watermark_removed.mp4",
                    "mode": "intensive",
                    "keep_intermediates": False,
                    "external_ref": "order-20260807-001",
                    "metadata": {},
                }
            ]
        },
    )

    source_uri: str = Field(min_length=1, max_length=4096)
    filename: str = Field(
        default="watermark_removed.mp4",
        min_length=1,
        max_length=256,
        pattern=r"^[^/\\]+\.mp4$",
    )
    mode: Literal["light", "intensive"] = "light"
    keep_intermediates: bool = False
    external_ref: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    campaign_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoUpscaleUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{
            "source_uri": "https://storage.example.com/video/source.mp4",
            "provider": "auto",
            "max_resolution": 1920,
            "external_ref": "order-20260907-001",
            "metadata": {},
        }]},
    )

    source_uri: str = Field(min_length=1, max_length=4096)
    provider: Literal["auto", "flashvsr", "flashvsr_v2", "seedvr2"] = "auto"
    max_resolution: int = Field(default=1920, ge=480, le=3840)
    external_ref: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DepthUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_uri": "https://storage.example.com/video/source.mp4",
                    "version": "da2",
                    "model": "small",
                    "filename": "depth.mp4",
                    "input_size": 518,
                    "max_resolution": 960,
                    "target_fps": -1,
                    "external_ref": "order-20260820-001",
                    "metadata": {},
                }
            ]
        },
    )

    source_uri: str = Field(min_length=1, max_length=4096)
    version: Literal["da2", "da3"] = Field(
        default="da2",
        description="深度模型版本：da2 为 Video Depth Anything，da3 为 Depth Anything 3。",
    )
    model: Literal["small", "base"] = Field(
        default="small",
        description="模型规模；Small 显存更低、速度更快，Base 细节更强。",
    )
    filename: str = Field(
        default="depth.mp4",
        min_length=1,
        max_length=256,
        pattern=r"^[^/\\]+\.mp4$",
    )
    input_size: int = Field(default=518, ge=224, le=756, multiple_of=14)
    max_resolution: int = Field(default=960, ge=224, le=1920)
    target_fps: float = Field(
        default=-1,
        ge=-1,
        le=60,
        description="-1 保持原始帧率，正数表示抽帧后的目标帧率。",
    )
    external_ref: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    campaign_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_fps")
    @classmethod
    def validate_target_fps(cls, value: float) -> float:
        if value != -1 and value < 1:
            raise ValueError("target_fps must be -1 or between 1 and 60")
        return value


class AudioSeparationUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_uri": "https://storage.example.com/video/source.mp4",
                    "model": "bandit-v2-multilingual",
                    "filename_prefix": "separated",
                    "external_ref": "order-20260820-001",
                    "metadata": {},
                }
            ]
        },
    )

    source_uri: str = Field(
        min_length=1,
        max_length=4096,
        description="公网HTTPS音频或视频URL；支持签名查询参数。",
    )
    model: Literal["bandit-v2-multilingual"] = Field(
        default="bandit-v2-multilingual",
        description="影视对白、音乐和音效三轨分离模型。",
    )
    filename_prefix: str = Field(
        default="separated",
        min_length=1,
        max_length=128,
        pattern=r"^[^/\\]+$",
        description="四个WAV结果文件的名称前缀。",
    )
    external_ref: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    campaign_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ColorGradeUrlJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{
                "video_url": "https://storage.example.com/video/source.mp4",
                "cube_url": "https://storage.example.com/luts/warm.cube",
                "strength": 0.65,
                "filename": "warm_graded.mp4",
            }],
        },
    )

    video_url: str = Field(min_length=1, max_length=4096, description="公网 HTTPS 视频 URL。")
    cube_url: str = Field(min_length=1, max_length=4096, description="公网 HTTPS .cube LUT URL。")
    strength: float = Field(default=0.65, ge=0.0, le=1.0, description="LUT 强度；0 为原片，1 为完整 LUT。")
    filename: str = Field(default="color_graded.mp4", min_length=1, max_length=256, pattern=r"^[^/\\]+\.mp4$")
    external_ref: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    campaign_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="seedance-2.0", min_length=1, max_length=128)
    channel: Literal["jmapi", "libtv", "auto"] = "jmapi"
    prompt: str = Field(min_length=1, max_length=10000)
    reference_image_urls: list[str] = Field(default_factory=list, max_length=9)
    reference_video_urls: list[str] = Field(default_factory=list, max_length=3)
    reference_audio_urls: list[str] = Field(default_factory=list, max_length=3)
    duration_seconds: int = Field(default=5, ge=2, le=15)
    resolution: Literal["480p", "720p", "1080p", "2K"] = "720p"
    aspect_ratio: Literal["9:16", "16:9", "1:1", "4:3", "3:4"] = "9:16"
    sound: bool = False
    external_ref: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["gpt-image-2","gpt-image-2.5","gpt-image-2.5-sunburst","gpt-image-2.5-flare","nano-banana-2"] = "gpt-image-2"
    channel: Literal["grsai"] = "grsai"
    prompt: str = Field(min_length=1,max_length=10000)
    reference_image_urls: list[str] = Field(default_factory=list,max_length=9)
    aspect_ratio: Literal["1:1","2:3","3:2","3:4","4:3","9:16","16:9"] = "1:1"
    image_size: Literal["1K","2K","4K"] = "1K"
    external_ref: str | None = Field(default=None,max_length=256)
    metadata: dict[str,Any] = Field(default_factory=dict)


class ChannelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1,max_length=100)
    code: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    deployment_type: Literal["local","third_party"]
    adapter: Literal["jmapi","libtv","grsai","local_h3"]
    base_url: str = Field(default="",max_length=2048)
    credential: str | None = Field(default=None,max_length=8192)
    auth_type: Literal["none","bearer","x-api-key"] = "none"
    priority: int = Field(default=100,ge=1,le=1000)
    timeout_seconds: int = Field(default=1800,ge=5,le=14400)


class ChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None,min_length=1,max_length=100)
    deployment_type: Literal["local","third_party"] | None = None
    adapter: Literal["jmapi","libtv","grsai","local_h3"] | None = None
    base_url: str | None = Field(default=None,max_length=2048)
    credential: str | None = Field(default=None,max_length=8192)
    auth_type: Literal["none","bearer","x-api-key"] | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None,ge=1,le=1000)
    timeout_seconds: int | None = Field(default=None,ge=5,le=14400)


class H3VideoJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{
                "prompt": "一只雄鹰在黑夜中飞过雪山，电影级光影",
                "duration_seconds": 5,
                "quality": "medium",
                "resolution": "480p",
                "aspect_ratio": "9:16",
                "priority": 500,
                "seed": 482901731,
                "external_ref": "order-001",
                "metadata": {},
            }, {
                "reference_video_urls": ["https://storage.example.com/reference.mp4"],
                "reference_image_urls": ["https://storage.example.com/identity.png"],
                "reference_audio_urls": ["https://storage.example.com/reference.mp3"],
                "prompt": "完整MiniMax H3提示词",
                "duration_seconds": 5,
                "quality": "medium",
                "resolution": "480p",
                "aspect_ratio": "9:16",
                "priority": 500,
                "seed": 482901731,
                "external_ref": "order-002",
                "metadata": {},
            }]
        },
    )

    reference_video_urls: list[str] = Field(default_factory=list, max_length=8)
    reference_image_urls: list[str] = Field(default_factory=list, max_length=8)
    reference_audio_urls: list[str] = Field(default_factory=list, max_length=8)
    prompt: str = Field(min_length=1, max_length=20_000)
    duration_seconds: float = Field(default=5, ge=2, le=15)
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    quality: Literal["low", "medium", "midia", "high"] = Field(
        default="medium",
        description="质量路由：low快速预览；medium默认生产；high质量采样。midia兼容为medium。",
    )
    aspect_ratio: Literal["9:16", "16:9", "1:1", "4:3", "3:4"] = "9:16"
    width: int | None = Field(default=None, ge=32, le=1344, multiple_of=32)
    height: int | None = Field(default=None, ge=32, le=1344, multiple_of=32)
    seed: int = Field(default=482901731, ge=0, le=18_446_744_073_709_551_615)
    priority: int = Field(
        default=500,
        ge=1,
        le=1000,
        description="任务优先级，数值越高越先分配空闲Worker；不会中断已运行任务。",
    )
    external_ref: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_single_reference_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for singular, plural in (
            ("reference_video_url", "reference_video_urls"),
            ("reference_image_url", "reference_image_urls"),
            ("identity_image_url", "reference_image_urls"),
            ("reference_audio_url", "reference_audio_urls"),
        ):
            legacy = data.pop(singular, None)
            if legacy and plural not in data:
                data[plural] = [legacy]
        if any(field in data for field in ("prompts", "segment_mode", "split_seconds")):
            raise ValueError("segment fields were removed; submit one atomic job with prompt")
        return data

    @field_validator("reference_video_urls", "reference_image_urls", "reference_audio_urls")
    @classmethod
    def validate_reference_urls(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("reference URL lists must not contain duplicates")
        if any(len(value) > 4096 for value in cleaned):
            raise ValueError("reference URL is too long")
        return cleaned

    @field_validator("quality")
    @classmethod
    def normalize_quality(cls, value: str) -> str:
        return "medium" if value == "midia" else value

    @model_validator(mode="after")
    def validate_h3_request(self):
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        resolve_dimensions(self.resolution, self.aspect_ratio, self.width, self.height, self.quality)
        return self


class H3WorkerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    base_url: str = Field(min_length=1, max_length=4096)
    enabled: bool = True


class H3WorkerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    base_url: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: bool | None = None
    draining: bool | None = None


class H3ManagedWorkerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_type: Literal["4090_24g", "4090_48g", "5090_32g"] = "5090_32g"
    provider_mode: Literal["spot", "deployment"] | None = None


class H3PoolConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_workers: int = Field(ge=0, le=100)
    max_workers: int = Field(ge=1, le=100)
    idle_timeout_seconds: int = Field(ge=60, le=604800)
    default_machine_type: Literal["4090_24g", "4090_48g", "5090_32g"] = "5090_32g"
    provider_mode: Literal["spot", "deployment"] = "spot"
    spot_estimated_exec_seconds: int = Field(default=82800, ge=1, le=86400)
    autoscaling_enabled: bool = True

    @model_validator(mode="after")
    def validate_limits(self):
        if self.min_workers > self.max_workers:
            raise ValueError("min_workers must not exceed max_workers")
        return self


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    quota: int | None = Field(default=None, ge=1, le=1_000_000_000)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def future_expiration(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return value


class ApiKeyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    quota: int | None = Field(default=None, ge=1, le=1_000_000_000)
    expires_at: datetime | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def non_blank_name(cls,value:str|None)->str|None:
        if value is not None and not value.strip():raise ValueError("name must not be blank")
        return value.strip() if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def future_expiration(cls,value:datetime|None)->datetime|None:
        if value is None:return value
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        if value<=datetime.now(timezone.utc):raise ValueError("expires_at must be in the future")
        return value


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    reference_wav_path: str | None = None
    prompt_wav_path: str | None = None
    prompt_text: str | None = None
    cfg_value: float = Field(default=2.0, ge=1.0, le=3.0)
    inference_timesteps: int = Field(default=10, ge=1, le=50)


class SubtitleDetectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_path: str = Field(min_length=1, max_length=4096)
    output_dir: str | None = Field(default=None, max_length=4096)
    source_lang_hint: str | None = Field(default=None, max_length=16)
    mode: str = Field(default="balanced", pattern=r"^(fast|balanced|accurate)$")
    export_debug_video: bool = True
    asr_segments: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class InternalOCRImage(BaseModel):
    image_id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=4096)
    regions: list[dict[str, Any]] = Field(default_factory=list)


class InternalOCRBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=256)
    source_lang_hint: str | None = Field(default=None, max_length=16)
    images: list[InternalOCRImage] = Field(min_length=1, max_length=20)


@lru_cache(maxsize=1)
def get_api_key_store() -> ApiKeyStore:
    return ApiKeyStore(get_settings().api_keys_db_path)


@lru_cache(maxsize=1)
def get_capability_store() -> CapabilityStore:
    settings=get_settings()
    return CapabilityStore(settings.ai_capabilities_db_path,settings.service_token)


@lru_cache(maxsize=1)
def get_generation_jobs() -> GenerationJobClient:
    return GenerationJobClient(get_settings(),get_capability_store())


def require_service_token(authorization: str = Header(default="")) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must use Bearer <API_KEY>",
        )
    token = authorization[7:].strip()
    if secrets.compare_digest(token, get_settings().service_token):
        return
    accepted, reason = get_api_key_store().authenticate_and_consume(token)
    if not accepted:
        code = status.HTTP_429_TOO_MANY_REQUESTS if reason == "quota_exceeded" else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=f"api key {reason}")


@lru_cache(maxsize=1)
def get_gpu_controller() -> GpuController:
    settings = get_settings()
    return GpuController(settings.control_runtime_dir / "gpu-state.json")


@lru_cache(maxsize=1)
def get_upstreams() -> AudioUpstreams:
    settings = get_settings()
    return AudioUpstreams(
        settings.asr_backend_url,
        settings.tts_backend_url,
        settings.upstream_timeout_seconds,
        settings.musetalk_backend_url,
    )


@lru_cache(maxsize=1)
def get_lipsync_client() -> LipSyncClient:
    settings = get_settings()
    return LipSyncClient(
        settings.musetalk_backend_url,
        settings.service_token,
        settings.musetalk_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_tts_jobs() -> TTSJobClient:
    return TTSJobClient(get_settings())


@lru_cache(maxsize=1)
def get_tts_quality_store() -> TTSQualityStore:
    return TTSQualityStore(get_settings())


_background_tasks: set[asyncio.Task[Any]] = set()


def _run_background(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@lru_cache(maxsize=1)
def get_scene_jobs() -> SceneJobClient:
    return SceneJobClient(get_settings())


@lru_cache(maxsize=1)
def get_video_review_jobs() -> VideoReviewJobClient:
    return VideoReviewJobClient(get_settings())


@lru_cache(maxsize=1)
def get_watermark_jobs() -> WatermarkJobClient:
    return WatermarkJobClient(get_settings())


@lru_cache(maxsize=1)
def get_depth_jobs() -> DepthJobClient:
    return DepthJobClient(get_settings())


@lru_cache(maxsize=1)
def get_audio_separation_jobs() -> AudioSeparationJobClient:
    return AudioSeparationJobClient(get_settings())


@lru_cache(maxsize=1)
def get_color_grade_jobs() -> ColorGradeJobClient:
    return ColorGradeJobClient(get_settings())


@lru_cache(maxsize=1)
def get_video_upscale_jobs() -> VideoUpscaleJobClient:
    return VideoUpscaleJobClient(get_settings())


@lru_cache(maxsize=1)
def get_h3_jobs() -> H3JobClient:
    return H3JobClient(get_settings())


@lru_cache(maxsize=1)
def get_h3_store() -> H3Store:
    return H3Store(get_settings().h3_db_path)


@lru_cache(maxsize=1)
def get_h3_scheduler() -> H3Scheduler:
    return H3Scheduler(get_settings(), get_h3_store())


def get_h3_pool() -> H3PoolManager:
    return H3PoolManager(get_settings(), get_h3_store())


async def _reconcile_observability_task(task: dict[str, Any]) -> None:
    service = str(task["service"])
    external_id = str(task["external_task_id"])
    if service == "lipsync":
        payload = await get_lipsync_client().status(external_id)
    elif service == "tts":
        status_payload = await asyncio.to_thread(get_tts_jobs().status, external_id)
        payload = status_payload.model_dump(mode="json")
    elif service == "scene":
        payload = await asyncio.to_thread(get_scene_jobs().status, external_id)
    elif service == "video_review":
        payload = await asyncio.to_thread(get_video_review_jobs().status, external_id)
    elif service == "watermark":
        payload = await asyncio.to_thread(get_watermark_jobs().status, external_id)
    elif service == "depth":
        payload = await asyncio.to_thread(get_depth_jobs().status, external_id)
    elif service == "separation":
        payload = await asyncio.to_thread(
            get_audio_separation_jobs().status, external_id
        )
    elif service == "upscale":
        payload = await asyncio.to_thread(get_video_upscale_jobs().status, external_id)
    elif service == "face":
        payload = await _face_request("GET", f"/v1/face-mosaic/jobs/{external_id}")
    elif service == "h3":
        payload = await asyncio.to_thread(get_h3_jobs().status, external_id)
    elif service in {"video_generation","image_generation"}:
        payload = await asyncio.to_thread(get_generation_jobs().status, external_id)
    else:
        return
    await asyncio.to_thread(
        get_observability_store().reconcile_task,
        service,
        external_id,
        payload,
    )


async def _observability_reconcile_loop() -> None:
    cleanup_at = 0.0
    try:
        await asyncio.to_thread(
            get_observability_store().backfill_task_timings,
            "h3",
            get_h3_store().observability_backfill_rows(),
        )
    except Exception:
        pass
    while True:
        try:
            settings = get_settings()
            active = await asyncio.to_thread(get_observability_store().active_tasks)
            if active:
                await asyncio.gather(
                    *(_reconcile_observability_task(task) for task in active),
                    return_exceptions=True,
                )
            loop_time = asyncio.get_running_loop().time()
            if loop_time >= cleanup_at:
                await asyncio.to_thread(get_observability_store().cleanup)
                cleanup_at = loop_time + 24 * 60 * 60
            await asyncio.sleep(max(1.0, settings.observability_reconcile_seconds))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)


async def _h3_health_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(get_h3_scheduler().probe_all)
            await asyncio.sleep(max(5.0, get_settings().h3_health_interval_seconds))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(10)


async def _h3_pool_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(get_h3_pool().reconcile)
            await asyncio.sleep(max(5.0, get_settings().h3_pool_reconcile_seconds))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(10)


async def _health_monitor_loop() -> None:
    """Run low-cost probes independently of API traffic.

    A randomized startup delay prevents multiple deployments from probing at the
    same wall-clock instant. L2/paid probes are deliberately not represented as
    successes until their fixed probe assets and task runners are configured.
    """
    settings = get_settings()
    await asyncio.sleep(random.uniform(0, max(0, settings.health_monitor_jitter_seconds)))
    next_l2 = asyncio.get_running_loop().time() + max(300, settings.health_monitor_l2_seconds)
    next_paid = asyncio.get_running_loop().time() + max(3600, settings.health_monitor_paid_seconds)
    while True:
        try:
            await get_health_monitor().run_l1()
            now = asyncio.get_running_loop().time()
            if now >= next_l2:
                await get_health_monitor().run_l2(False)
                next_l2 = now + max(300, settings.health_monitor_l2_seconds)
            if now >= next_paid:
                await get_health_monitor().run_l2(True)
                next_paid = now + max(3600, settings.health_monitor_paid_seconds)
            await asyncio.sleep(max(60, settings.health_monitor_l1_seconds))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)


@app.on_event("startup")
async def start_observability_reconciler() -> None:
    _run_background(_observability_reconcile_loop())
    _run_background(_h3_health_loop())
    _run_background(_health_monitor_loop())
    _run_background(_h3_pool_loop())


@app.on_event("shutdown")
async def stop_background_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@app.get("/health", tags=["系统状态"], summary="检查中台与上游服务健康状态")
async def health() -> dict[str, Any]:
    upstreams = await get_upstreams().health()
    healthy = all(item["status"] == "ok" for item in upstreams.values())
    return {
        "status": "ok" if healthy else "degraded",
        "upstreams": upstreams,
        "gpus": await asyncio.to_thread(get_gpu_controller().all_states),
        "observability": await asyncio.to_thread(get_observability_store().health),
    }


@app.post(
    "/v1/asr/transcriptions",
    tags=["语音识别"],
    summary="通过音视频 URL 识别文字",
    dependencies=[Depends(require_service_token)],
)
async def transcribe(
    request: ASRUrlRequest,
) -> dict[str, Any]:
    settings = get_settings()
    directory = settings.remote_input_dir / f"asr-{uuid4().hex}"
    try:
        media = await download_public_media_async(
            request.file_url,
            directory,
            "source",
            ASR_MEDIA,
            MAX_UPLOAD_BYTES,
            settings.upstream_timeout_seconds,
        )
        content = await asyncio.to_thread(media.path.read_bytes)
        return await _transcribe_content(
            media.path.name,
            content,
            request.language,
            request.beam_size,
        )
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, True)


@app.post(
    "/v1/asr/transcriptions/upload",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def transcribe_upload(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    beam_size: int = Form(default=5, ge=1, le=10),
) -> dict[str, Any]:
    filename = file.filename or "audio.wav"
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="upload is too large")
    if not content:
        raise HTTPException(status_code=400, detail="empty audio file")
    detected_suffix = sniff_media_suffix(bytes(content[:64]))
    if detected_suffix not in ASR_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail="uploaded file header is not a supported audio or video format",
        )
    normalized_filename = f"{Path(filename).stem or 'audio'}{detected_suffix}"
    return await _transcribe_content(
        normalized_filename,
        bytes(content),
        language,
        beam_size,
    )


async def _transcribe_content(
    filename: str,
    content: bytes,
    language: str | None,
    beam_size: int,
) -> dict[str, Any]:
    try:
        return await get_upstreams().transcribe(filename, content, language, beam_size)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ASR backend failed: {exc}") from exc


@app.post(
    "/v1/tts/speech",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def synthesize(request: TTSRequest) -> Response:
    try:
        content, media_type, headers = await get_upstreams().synthesize(
            request.model_dump(exclude_none=True)
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS backend failed: {exc}") from exc
    return Response(content=content, media_type=media_type, headers=headers)


@app.post(
    "/v1/lipsync/jobs",
    tags=["唇形驱动"],
    summary="通过视频和音频 URL 创建唇形驱动任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_lipsync_job(
    request: LipSyncUrlRequest,
) -> dict[str, Any]:
    try:
        return await get_lipsync_client().submit_urls(
            request.video_url,
            request.audio_url,
            request.face_restore,
        )
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.post(
    "/v1/lipsync/jobs/upload",
    include_in_schema=False,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_lipsync_upload_job(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    face_restore: bool = Form(default=False),
) -> dict[str, Any]:
    try:
        return await get_lipsync_client().submit_upload(
            video.filename or "video.mp4",
            video.file,
            audio.filename or "audio.wav",
            audio.file,
            face_restore,
        )
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.get(
    "/v1/lipsync/jobs",
    tags=["唇形驱动"],
    summary="查询唇形驱动任务列表",
    dependencies=[Depends(require_service_token)],
)
async def list_lipsync_jobs(
    limit: int = 50,
    state: str | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    try:
        return await get_lipsync_client().list_jobs(limit, state)
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.get(
    "/v1/lipsync/jobs/{job_id}",
    tags=["唇形驱动"],
    summary="查询唇形驱动任务状态",
    dependencies=[Depends(require_service_token)],
)
async def get_lipsync_job(job_id: str) -> dict[str, Any]:
    try:
        return await get_lipsync_client().status(job_id)
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.get(
    "/v1/lipsync/jobs/{job_id}/video",
    tags=["唇形驱动"],
    summary="下载唇形驱动结果视频",
    dependencies=[Depends(require_service_token)],
)
async def get_lipsync_video(job_id: str) -> Response:
    try:
        content, headers = await get_lipsync_client().video(job_id)
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(content=content, media_type="video/mp4", headers=headers)


@app.get(
    "/v1/lipsync/jobs/{job_id}/logs",
    tags=["唇形驱动"],
    summary="查询唇形驱动任务日志",
    dependencies=[Depends(require_service_token)],
)
async def get_lipsync_logs(
    job_id: str,
    stage: str = "musetalk",
    tail: int = 200,
) -> dict[str, Any]:
    if stage not in {"musetalk", "gfpgan"}:
        raise HTTPException(status_code=422, detail="unsupported log stage")
    if tail < 1 or tail > 500:
        raise HTTPException(status_code=422, detail="tail must be between 1 and 500")
    try:
        return await get_lipsync_client().logs(job_id, stage, tail)
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.post(
    "/v1/lipsync/jobs/{job_id}/cancel",
    tags=["唇形驱动"],
    summary="取消唇形驱动任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_lipsync_job(job_id: str) -> dict[str, Any]:
    try:
        return await get_lipsync_client().cancel(job_id)
    except LipSyncUpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.post(
    "/v1/ocr/batch",
    tags=["OCR 文字识别"],
    summary="通过图片 URL 批量识别文字",
    dependencies=[Depends(require_service_token)],
)
async def ocr_url_batch(request: OCRUrlBatchRequest) -> Response:
    settings = get_settings()
    request_id = request.job_id or str(uuid4())
    directory = settings.remote_input_dir / f"ocr-{uuid4().hex}"
    try:
        downloads = await asyncio.gather(
            *(
                download_public_media_async(
                    image.url,
                    directory,
                    f"image-{index + 1:02d}",
                    IMAGE_MEDIA,
                    MAX_OCR_IMAGE_BYTES,
                    settings.upstream_timeout_seconds,
                )
                for index, image in enumerate(request.images)
            ),
            return_exceptions=True,
        )
        failure = next((item for item in downloads if isinstance(item, Exception)), None)
        if failure:
            if isinstance(failure, MediaFetchError):
                raise HTTPException(failure.status_code, failure.detail) from failure
            raise HTTPException(502, "unable to download OCR image") from failure
        payload = {
            "job_id": request_id,
            "source_lang_hint": request.source_lang_hint,
            "images": [
                {
                    "image_id": image.image_id,
                    "path": media.path.as_posix(),
                    "time": image.time,
                    "regions": [region.model_dump() for region in image.regions],
                }
                for image, media in zip(request.images, downloads, strict=True)
            ],
        }
        try:
            async with httpx.AsyncClient(
                timeout=settings.upstream_timeout_seconds,
                trust_env=False,
            ) as client:
                upstream = await client.post(
                    f"{settings.ocr_gateway_url.rstrip('/')}/v1/ocr/batch",
                    json=payload,
                )
        except httpx.RequestError as exc:
            raise HTTPException(503, "OCR backend is unavailable") from exc
        response_headers = {}
        if worker := upstream.headers.get("x-ocr-worker"):
            response_headers["X-OCR-Worker"] = worker
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers=response_headers,
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, True)


@app.post(
    "/internal/admin/subtitle/detect",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def detect_subtitle_events(request: SubtitleDetectRequest) -> Response:
    allowed_root = Path("/home/donxu/ai-centre/runtime").resolve()
    input_path = Path(request.input_path).resolve()
    if not input_path.is_relative_to(allowed_root):
        raise HTTPException(status_code=422, detail="input_path must be inside the runtime directory")
    if request.output_dir:
        output_path = Path(request.output_dir).resolve()
        if not output_path.is_relative_to(allowed_root):
            raise HTTPException(status_code=422, detail="output_dir must be inside the runtime directory")
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().upstream_timeout_seconds,
            trust_env=False,
        ) as client:
            upstream = await client.post(
                f"{get_settings().subtitle_api_url.rstrip('/')}/v1/subtitle-events/detect",
                json=request.model_dump(),
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="subtitle backend is unavailable") from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.post(
    "/internal/admin/ocr/batch",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def ocr_internal_batch(request: InternalOCRBatchRequest) -> Response:
    allowed_root = Path("/home/donxu/ai-centre/runtime/admin/uploads").resolve()
    for image in request.images:
        path = Path(image.path).resolve()
        if not path.is_relative_to(allowed_root) or not path.is_file():
            raise HTTPException(status_code=422, detail="OCR image path is outside the admin upload directory")
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().upstream_timeout_seconds,
            trust_env=False,
        ) as client:
            upstream = await client.post(
                f"{get_settings().ocr_gateway_url.rstrip('/')}/v1/ocr/batch",
                json=request.model_dump(),
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="OCR backend is unavailable") from exc
    headers = {}
    if worker := upstream.headers.get("x-ocr-worker"):
        headers["X-OCR-Worker"] = worker
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers=headers,
    )


async def _face_request(method: str, path: str, json_body=None) -> dict[str, Any]:
    settings = get_settings()
    request_kwargs = {"json": json_body} if json_body is not None else {}
    timeout_seconds = (
        settings.face_wait_timeout_seconds + 30
        if path == "/v1/face-mosaic/jobs/wait"
        else settings.upstream_timeout_seconds
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=15),
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                f"{settings.face_api_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {settings.service_token}"},
                **request_kwargs,
            )
    except httpx.RequestError as exc:
        raise HTTPException(503, "Face backend is unavailable") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Face backend returned an invalid response") from exc
    if not response.is_success:
        detail = payload.get("detail", "Face backend request failed")
        raise HTTPException(response.status_code, detail)
    return payload


@app.post(
    "/v1/face-mosaic/jobs",
    tags=["人脸处理"],
    summary="通过视频 URL 创建人脸处理任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_face_url_job(request: FaceUrlJobRequest) -> dict[str, Any]:
    try:
        await asyncio.to_thread(validate_public_https_url, request.source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return await _face_request("POST", "/v1/face-mosaic/jobs", request.model_dump())


@app.post(
    "/v1/face-mosaic/jobs/wait",
    tags=["人脸处理"],
    summary="提交人脸处理并等待 OSS 结果",
    dependencies=[Depends(require_service_token)],
)
async def create_face_url_job_and_wait(request: FaceUrlJobRequest) -> dict[str, Any]:
    try:
        await asyncio.to_thread(validate_public_https_url, request.source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return await _face_request(
        "POST",
        "/v1/face-mosaic/jobs/wait",
        request.model_dump(),
    )


@app.get(
    "/v1/face-mosaic/jobs/{job_id}",
    tags=["人脸处理"],
    summary="查询人脸处理任务状态",
    dependencies=[Depends(require_service_token)],
)
async def get_face_url_job(job_id: str) -> dict[str, Any]:
    return await _face_request("GET", f"/v1/face-mosaic/jobs/{job_id}")


@app.post(
    "/v1/face-mosaic/jobs/{job_id}/cancel",
    tags=["人脸处理"],
    summary="取消人脸处理任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_face_url_job(job_id: str) -> dict[str, Any]:
    return await _face_request("POST", f"/v1/face-mosaic/jobs/{job_id}/cancel")


async def _validate_scene_source(source_uri: str) -> None:
    try:
        await asyncio.to_thread(validate_public_https_url, source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/video-scenes/jobs",
    tags=["视频切片"],
    summary="创建异步 SceneDetect 视频切片任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_scene_job(request: SceneUrlJobRequest) -> dict[str, Any]:
    await _validate_scene_source(request.source_uri)
    priority = 0 if request.metadata.get("probe") is True else 5
    try:
        job = await asyncio.to_thread(
            get_scene_jobs().submit,
            request.model_dump(),
            priority,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue scene detection job: {type(exc).__name__}",
        ) from exc
    return {
        "job_id": job.id,
        "status": "queued",
        "priority": priority,
        "status_url": f"/v1/video-scenes/jobs/{job.id}",
    }


@app.post(
    "/v1/video-scenes/jobs/wait",
    tags=["视频切片"],
    summary="高优先级提交视频切片并等待 OSS 结果",
    dependencies=[Depends(require_service_token)],
)
async def create_scene_job_and_wait(request: SceneUrlJobRequest) -> dict[str, Any]:
    await _validate_scene_source(request.source_uri)
    try:
        job = await asyncio.to_thread(
            get_scene_jobs().submit,
            request.model_dump(),
            9,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue scene detection job: {type(exc).__name__}",
        ) from exc
    try:
        result = await asyncio.to_thread(
            job.get,
            timeout=get_settings().scene_wait_timeout_seconds,
        )
    except CeleryTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": "scene detection is still running",
                "job_id": job.id,
                "status_url": f"/v1/video-scenes/jobs/{job.id}",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"scene detection failed: {type(exc).__name__}",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="scene worker returned an invalid result",
        )
    return result


@app.get(
    "/v1/video-scenes/jobs/{job_id}",
    tags=["视频切片"],
    summary="查询视频切片任务",
    dependencies=[Depends(require_service_token)],
)
async def get_scene_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_scene_jobs().status, str(job_id))
    except SceneJobNotFound as exc:
        raise HTTPException(status_code=404, detail="scene job not found") from exc


@app.post(
    "/v1/video-scenes/jobs/{job_id}/cancel",
    tags=["视频切片"],
    summary="取消视频切片任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_scene_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_scene_jobs().cancel, str(job_id))
    except SceneJobNotFound as exc:
        raise HTTPException(status_code=404, detail="scene job not found") from exc


async def _validate_video_review_source(video_url: str, references: list[dict[str, Any]]) -> None:
    urls = [video_url, *[str(item.get("url") or "") for item in references]]
    try:
        for url in urls:
            if url:
                await asyncio.to_thread(validate_public_https_url, url)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/video-reviews/jobs",
    tags=["AI 视频拉片"],
    summary="创建无参考 AI 视频拉片与拆审任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_video_review_job(request: VideoReviewUrlJobRequest) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    if payload.get("reference_assets") and not payload.get("continuity_check"):
        raise HTTPException(status_code=422, detail="reference_assets requires continuity_check=true")
    await _validate_video_review_source(payload["video_url"], payload.get("reference_assets", []))
    try:
        job = await asyncio.to_thread(
            get_video_review_jobs().submit,
            payload,
            0 if payload.get("metadata", {}).get("probe") is True else 5,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"unable to enqueue video review job: {type(exc).__name__}") from exc
    return {
        "job_id": job.id,
        "status": "queued",
        "status_url": f"/v1/video-reviews/jobs/{job.id}",
        "report_url": f"/v1/video-reviews/jobs/{job.id}/report",
    }


@app.get(
    "/v1/video-reviews/jobs/{job_id}",
    tags=["AI 视频拉片"],
    summary="查询 AI 视频拉片任务",
    dependencies=[Depends(require_service_token)],
)
async def get_video_review_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_video_review_jobs().status, str(job_id))
    except VideoReviewJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video review job not found") from exc


@app.get(
    "/v1/video-reviews/jobs/{job_id}/report",
    tags=["AI 视频拉片"],
    summary="获取 AI 视频拉片报告",
    dependencies=[Depends(require_service_token)],
)
async def get_video_review_report(job_id: UUID, format: Literal["json", "markdown", "html"] = "json") -> Any:
    job_key = str(job_id)
    try:
        await asyncio.to_thread(get_video_review_jobs().status, job_key)
    except VideoReviewJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video review job not found") from exc
    filename = {"markdown": "report.md", "html": "report.html", "json": "report.json"}[format]
    path = get_settings().video_review_work_dir / job_key / filename
    if not path.is_file():
        raise HTTPException(status_code=409, detail="video review report is not ready")
    if format in {"markdown", "html"}:
        media_type = "text/html; charset=utf-8" if format == "html" else "text/markdown; charset=utf-8"
        suffix = "html" if format == "html" else "md"
        return FileResponse(path, media_type=media_type, filename=f"video-review-{job_key}.{suffix}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="video review report is unreadable") from exc


@app.post(
    "/v1/video-reviews/jobs/{job_id}/cancel",
    tags=["AI 视频拉片"],
    summary="取消 AI 视频拉片任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_video_review_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_video_review_jobs().cancel, str(job_id))
    except VideoReviewJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video review job not found") from exc


async def _validate_watermark_source(source_uri: str) -> None:
    try:
        await asyncio.to_thread(validate_public_https_url, source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/watermark-removal/jobs",
    tags=["水印处理"],
    summary="创建异步视频水印处理任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_watermark_job(request: WatermarkUrlJobRequest) -> dict[str, Any]:
    await _validate_watermark_source(request.source_uri)
    try:
        job = await asyncio.to_thread(
            get_watermark_jobs().submit,
            request.model_dump(),
            0 if request.metadata.get("probe") is True else 5,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue watermark removal job: {type(exc).__name__}",
        ) from exc
    return {
        "job_id": job.id,
        "status": "queued",
        "priority": 5,
        "status_url": f"/v1/watermark-removal/jobs/{job.id}",
    }


@app.post(
    "/v1/watermark-removal/jobs/wait",
    tags=["水印处理"],
    summary="同步等待视频水印处理结果",
    dependencies=[Depends(require_service_token)],
)
async def create_watermark_job_and_wait(
    request: WatermarkUrlJobRequest,
) -> dict[str, Any]:
    await _validate_watermark_source(request.source_uri)
    try:
        job = await asyncio.to_thread(
            get_watermark_jobs().submit,
            request.model_dump(),
            9,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue watermark removal job: {type(exc).__name__}",
        ) from exc
    try:
        result = await asyncio.to_thread(
            job.get,
            timeout=get_settings().watermark_wait_timeout_seconds,
        )
    except CeleryTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": "watermark removal is still running",
                "job_id": job.id,
                "status_url": f"/v1/watermark-removal/jobs/{job.id}",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"watermark removal failed: {type(exc).__name__}",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="watermark worker returned an invalid result",
        )
    return result


@app.get(
    "/v1/watermark-removal/jobs/{job_id}",
    tags=["水印处理"],
    summary="查询视频水印处理任务",
    dependencies=[Depends(require_service_token)],
)
async def get_watermark_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_watermark_jobs().status, str(job_id))
    except WatermarkJobNotFound as exc:
        raise HTTPException(status_code=404, detail="watermark job not found") from exc


@app.post(
    "/v1/watermark-removal/jobs/{job_id}/cancel",
    tags=["水印处理"],
    summary="取消视频水印处理任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_watermark_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_watermark_jobs().cancel, str(job_id))
    except WatermarkJobNotFound as exc:
        raise HTTPException(status_code=404, detail="watermark job not found") from exc


async def _validate_video_upscale_source(source_uri: str) -> None:
    try:
        await asyncio.to_thread(validate_public_https_url, source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/video-upscale/jobs",
    tags=["视频超分"],
    summary="创建智能渠道视频超分任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_video_upscale_job(request: VideoUpscaleUrlJobRequest) -> dict[str, Any]:
    await _validate_video_upscale_source(request.source_uri)
    try:
        job = await asyncio.to_thread(get_video_upscale_jobs().submit, request.model_dump(), 0 if request.metadata.get("probe") is True else 5)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue video upscale job: {type(exc).__name__}",
        ) from exc
    return {
        "job_id": job.id,
        "status": "queued",
        "provider": request.provider,
        "status_url": f"/v1/video-upscale/jobs/{job.id}",
    }


@app.post(
    "/v1/video-upscale/jobs/wait",
    tags=["视频超分"],
    summary="高优先级提交视频超分并等待智能渠道结果",
    dependencies=[Depends(require_service_token)],
)
async def create_video_upscale_job_and_wait(request: VideoUpscaleUrlJobRequest) -> dict[str, Any]:
    await _validate_video_upscale_source(request.source_uri)
    try:
        job = await asyncio.to_thread(get_video_upscale_jobs().submit, request.model_dump(), 9)
        result = await asyncio.to_thread(
            job.get, timeout=get_settings().video_upscale_wait_timeout_seconds
        )
    except CeleryTimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={"message": "video upscale is still running", "job_id": job.id,
                    "status_url": f"/v1/video-upscale/jobs/{job.id}"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"video upscale failed: {type(exc).__name__}",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="video upscale worker returned an invalid result")
    return result


@app.get(
    "/v1/video-upscale/jobs/{job_id}",
    tags=["视频超分"],
    summary="查询视频超分任务",
    dependencies=[Depends(require_service_token)],
)
async def get_video_upscale_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_video_upscale_jobs().status, str(job_id))
    except VideoUpscaleJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video upscale job not found") from exc


@app.post(
    "/v1/video-upscale/jobs/{job_id}/cancel",
    tags=["视频超分"],
    summary="取消视频超分任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_video_upscale_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_video_upscale_jobs().cancel, str(job_id))
    except VideoUpscaleJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video upscale job not found") from exc


async def _validate_depth_source(source_uri: str) -> None:
    try:
        await asyncio.to_thread(validate_public_https_url, source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/video-depth/jobs",
    tags=["视频深度推理"],
    summary="创建异步单目视频深度推理任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_depth_job(request: DepthUrlJobRequest) -> dict[str, Any]:
    await _validate_depth_source(request.source_uri)
    priority = 0 if request.metadata.get("probe") is True else 5
    try:
        job = await asyncio.to_thread(get_depth_jobs().submit, request.model_dump(), priority)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue video depth job: {type(exc).__name__}",
        ) from exc
    settings = get_settings()
    effective_input_size = (
        min(request.input_size, settings.depth_da2_base_max_input_size)
        if request.version == "da2" and request.model == "base"
        else request.input_size
    )
    return {
        "job_id": job.id,
        "status": "queued",
        "priority": priority,
        "version": request.version,
        "model": request.model,
        "requested_input_size": request.input_size,
        "effective_input_size": effective_input_size,
        "input_size_capped": effective_input_size != request.input_size,
        "status_url": f"/v1/video-depth/jobs/{job.id}",
    }


@app.post(
    "/v1/video-depth/jobs/wait",
    tags=["视频深度推理"],
    summary="高优先级提交视频深度推理并等待结果",
    dependencies=[Depends(require_service_token)],
)
async def create_depth_job_and_wait(request: DepthUrlJobRequest) -> dict[str, Any]:
    await _validate_depth_source(request.source_uri)
    try:
        job = await asyncio.to_thread(get_depth_jobs().submit, request.model_dump(), 9)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue video depth job: {type(exc).__name__}",
        ) from exc
    try:
        result = await asyncio.to_thread(
            job.get,
            timeout=get_settings().depth_wait_timeout_seconds,
        )
    except CeleryTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": "video depth inference is still running",
                "job_id": job.id,
                "status_url": f"/v1/video-depth/jobs/{job.id}",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"video depth inference failed: {type(exc).__name__}",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="video depth worker returned an invalid result",
        )
    return result


@app.get(
    "/v1/video-depth/jobs/{job_id}",
    tags=["视频深度推理"],
    summary="查询视频深度推理任务",
    dependencies=[Depends(require_service_token)],
)
async def get_depth_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_depth_jobs().status, str(job_id))
    except DepthJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video depth job not found") from exc


@app.post(
    "/v1/video-depth/jobs/{job_id}/cancel",
    tags=["视频深度推理"],
    summary="取消视频深度推理任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_depth_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_depth_jobs().cancel, str(job_id))
    except DepthJobNotFound as exc:
        raise HTTPException(status_code=404, detail="video depth job not found") from exc


async def _validate_audio_separation_source(source_uri: str) -> None:
    try:
        await asyncio.to_thread(validate_public_https_url, source_uri)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/audio-separation/jobs",
    tags=["音频分离"],
    summary="创建异步对白、音乐和音效分离任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_audio_separation_job(
    request: AudioSeparationUrlJobRequest,
) -> dict[str, Any]:
    await _validate_audio_separation_source(request.source_uri)
    priority = 0 if request.metadata.get("probe") is True else 5
    try:
        job = await asyncio.to_thread(
            get_audio_separation_jobs().submit,
            request.model_dump(),
            priority,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue audio separation job: {type(exc).__name__}",
        ) from exc
    return {
        "job_id": job.id,
        "status": "queued",
        "priority": priority,
        "model": request.model,
        "status_url": f"/v1/audio-separation/jobs/{job.id}",
    }


@app.post(
    "/v1/audio-separation/jobs/wait",
    tags=["音频分离"],
    summary="高优先级提交音频分离并等待四轨OSS结果",
    dependencies=[Depends(require_service_token)],
)
async def create_audio_separation_job_and_wait(
    request: AudioSeparationUrlJobRequest,
) -> dict[str, Any]:
    await _validate_audio_separation_source(request.source_uri)
    try:
        job = await asyncio.to_thread(
            get_audio_separation_jobs().submit,
            request.model_dump(),
            9,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue audio separation job: {type(exc).__name__}",
        ) from exc
    try:
        result = await asyncio.to_thread(
            job.get,
            timeout=get_settings().audio_separation_wait_timeout_seconds,
        )
    except CeleryTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": "audio separation is still running",
                "job_id": job.id,
                "status_url": f"/v1/audio-separation/jobs/{job.id}",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"audio separation failed: {type(exc).__name__}",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="audio separation worker returned an invalid result",
        )
    return result


@app.get(
    "/v1/audio-separation/jobs/{job_id}",
    tags=["音频分离"],
    summary="查询音频分离任务",
    dependencies=[Depends(require_service_token)],
)
async def get_audio_separation_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_audio_separation_jobs().status, str(job_id)
        )
    except AudioSeparationJobNotFound as exc:
        raise HTTPException(status_code=404, detail="audio separation job not found") from exc


@app.post(
    "/v1/audio-separation/jobs/{job_id}/cancel",
    tags=["音频分离"],
    summary="取消音频分离任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_audio_separation_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            get_audio_separation_jobs().cancel, str(job_id)
        )
    except AudioSeparationJobNotFound as exc:
        raise HTTPException(status_code=404, detail="audio separation job not found") from exc


async def _validate_color_grade_sources(request: ColorGradeUrlJobRequest) -> None:
    try:
        await asyncio.gather(
            asyncio.to_thread(validate_public_https_url, request.video_url),
            asyncio.to_thread(validate_public_https_url, request.cube_url),
        )
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/color-grade/jobs",
    tags=["视频调色"],
    summary="创建异步 .cube 视频调色任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_color_grade_job(request: ColorGradeUrlJobRequest) -> dict[str, Any]:
    await _validate_color_grade_sources(request)
    priority = 0 if request.metadata.get("probe") is True else 5
    try:
        job = await asyncio.to_thread(get_color_grade_jobs().submit, request.model_dump(), priority)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"unable to enqueue color grade job: {type(exc).__name__}") from exc
    return {
        "job_id": job.id,
        "status": "queued",
        "priority": priority,
        "strength": request.strength,
        "status_url": f"/v1/color-grade/jobs/{job.id}",
    }


@app.get(
    "/v1/color-grade/jobs/{job_id}",
    tags=["视频调色"],
    summary="查询视频调色任务",
    dependencies=[Depends(require_service_token)],
)
async def get_color_grade_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_color_grade_jobs().status, str(job_id))
    except ColorGradeJobNotFound as exc:
        raise HTTPException(status_code=404, detail="color grade job not found") from exc


@app.post(
    "/v1/color-grade/jobs/{job_id}/cancel",
    tags=["视频调色"],
    summary="取消视频调色任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_color_grade_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_color_grade_jobs().cancel, str(job_id))
    except ColorGradeJobNotFound as exc:
        raise HTTPException(status_code=404, detail="color grade job not found") from exc


def _transcription_language(transcription: dict[str, Any], text: str | None) -> str | None:
    detected = transcription.get("language")
    if isinstance(detected, str) and detected.strip():
        return normalize_language(detected)
    return infer_text_language(text or "")


def _transcription_speech_segments(
    transcription: dict[str, Any],
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for segment in transcription.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        start, end = segment.get("start"), segment.get("end")
        if isinstance(start, int | float) and isinstance(end, int | float) and end > start:
            result.append((float(start), float(end)))
    return result


async def _reference_transcription(
    reference_path: Path,
    prompt_text: str | None,
    target_language: str | None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    supplied = prompt_text.strip() if prompt_text and prompt_text.strip() else None
    supplied_language = infer_text_language(supplied or "")
    # An explicit, clearly classifiable transcript is already enough to route the
    # common same-language case and avoids a redundant ASR pass.
    if supplied and supplied_language and supplied_language == target_language:
        return supplied, supplied_language, {}
    content = await asyncio.to_thread(reference_path.read_bytes)
    transcription = await _transcribe_content(reference_path.name, content, None, 5)
    resolved = supplied or _transcription_text(transcription) or None
    return resolved, _transcription_language(transcription, resolved), transcription


async def _select_reference_window(
    reference_path: Path,
    transcription: dict[str, Any],
    directory: Path,
) -> Path:
    source = await asyncio.to_thread(reference_path.read_bytes)
    try:
        normalized, _ = await asyncio.to_thread(
            normalize_to_wav,
            source,
            get_settings().tts_ffmpeg_bin,
            REFERENCE_SAMPLE_RATE,
            REFERENCE_CHANNELS,
        )
    except Exception:
        # The public input was already signature-validated. Keep an otherwise
        # upstream-decodable reference usable if local ffmpeg lacks its codec.
        return reference_path
    normalized_path = directory / "reference-normalized.wav"
    await asyncio.to_thread(normalized_path.write_bytes, normalized)
    duration = reference_wav_duration_seconds(normalized)
    cache_key = hashlib.sha256(normalized).hexdigest()
    now = time.monotonic()
    cached_window = _REFERENCE_WINDOW_CACHE.get(cache_key)
    if (
        cached_window is not None
        and now - cached_window[0] <= REFERENCE_EMOTION_CACHE_TTL_SECONDS
    ):
        selected_start, selected_span = cached_window[1], cached_window[2]
        selected_audio = append_wav_silence(
            slice_wav(normalized, selected_start, selected_span),
            REFERENCE_TAIL_SILENCE_SECONDS,
        )
        selected_path = directory / "reference-selected.wav"
        await asyncio.to_thread(selected_path.write_bytes, selected_audio)
        (directory / "reference-window.json").write_text(
            json.dumps(
                {
                    "start_seconds": selected_start,
                    "speech_duration_seconds": selected_span,
                    "duration_seconds": selected_span
                    + REFERENCE_TAIL_SILENCE_SECONDS,
                }
            ),
            encoding="utf-8",
        )
        return selected_path
    speech_segments = wav_speech_segments(normalized)
    if not speech_segments:
        speech_segments = _transcription_speech_segments(transcription)
    ranges = reference_window_ranges(duration, speech_segments)

    async def score(start: float, span: float) -> tuple[float, float, float, bytes]:
        candidate = slice_wav(normalized, start, span)
        similarity = await _speaker_similarity(normalized_path, candidate)
        return (
            window_score(
                speaker_similarity=similarity,
                coverage=speech_coverage(
                    start,
                    min(duration, start + span),
                    speech_segments,
                ),
                unclipped=unclipped_score(candidate),
                start_seconds=start,
                duration_seconds=duration,
                window_seconds=span,
                snr_db=signal_to_noise_db(candidate),
            ),
            start,
            span,
            candidate,
        )

    candidates = await asyncio.gather(*(score(start, span) for start, span in ranges))
    _, selected_start, selected_span, selected_audio = max(
        candidates,
        key=lambda item: item[0],
    )
    _REFERENCE_WINDOW_CACHE[cache_key] = (now, selected_start, selected_span)
    if len(_REFERENCE_WINDOW_CACHE) > REFERENCE_EMOTION_CACHE_MAX_ENTRIES:
        oldest_key = min(
            _REFERENCE_WINDOW_CACHE,
            key=lambda key: _REFERENCE_WINDOW_CACHE[key][0],
        )
        _REFERENCE_WINDOW_CACHE.pop(oldest_key, None)
    selected_audio = append_wav_silence(
        selected_audio,
        REFERENCE_TAIL_SILENCE_SECONDS,
    )
    selected_path = directory / "reference-selected.wav"
    await asyncio.to_thread(selected_path.write_bytes, selected_audio)
    # Store only a non-sensitive local measurement for diagnostics.
    (directory / "reference-window.json").write_text(
        json.dumps(
            {
                "start_seconds": selected_start,
                "speech_duration_seconds": selected_span,
                "duration_seconds": selected_span + REFERENCE_TAIL_SILENCE_SECONDS,
            }
        ),
        encoding="utf-8",
    )
    return selected_path


def _canonical_detected_emotion(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    aliases = {
        "happy": ("happy", "开心", "高兴", "喜悦", "愉快"),
        "sad": ("sad", "悲伤", "难过", "伤感"),
        "angry": ("angry", "愤怒", "生气"),
        "fear": ("fear", "害怕", "恐惧", "紧张"),
        "surprise": ("surprise", "惊讶", "惊喜"),
        "neutral": ("neutral", "中性", "平静"),
    }
    return next(
        (name for name, words in aliases.items() if any(word in lowered for word in words)),
        None,
    )


async def _reference_emotion(reference_path: Path) -> str | None:
    try:
        content = await asyncio.to_thread(reference_path.read_bytes)
        cache_key = hashlib.sha256(content).hexdigest()
        now = time.monotonic()
        cached = _REFERENCE_EMOTION_CACHE.get(cache_key)
        if cached is not None and now - cached[0] <= REFERENCE_EMOTION_CACHE_TTL_SECONDS:
            return cached[1]
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10), trust_env=False
        ) as client:
            response = await client.post(
                f"{get_settings().emotion_verify_url.rstrip('/')}/v1/emotion",
                files={"audio": (reference_path.name, content, "audio/wav")},
            )
        response.raise_for_status()
        body = response.json()
        emotion = _canonical_detected_emotion(str(body.get("label") or ""))
        _REFERENCE_EMOTION_CACHE[cache_key] = (now, emotion)
        if len(_REFERENCE_EMOTION_CACHE) > REFERENCE_EMOTION_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _REFERENCE_EMOTION_CACHE,
                key=lambda key: _REFERENCE_EMOTION_CACHE[key][0],
            )
            _REFERENCE_EMOTION_CACHE.pop(oldest_key, None)
        return emotion
    except (OSError, TypeError, ValueError, httpx.HTTPError):
        return None


async def _prepare_clone_context(
    request: TTSPublicSpeechRequest,
    reference_path: Path | None,
    directory: Path | None,
) -> CloneContext:
    target_language = infer_text_language(request.text, request.language)
    if reference_path is None:
        emotion, status = await enhance_emotion(
            get_settings(), request.text, request.emotion, request.emotion_enhance
        )
        return CloneContext(
            target_language=target_language,
            emotion_strategy=request.emotion_strategy,
            style=build_style_instruction(emotion, request.prosody, cloning=False),
            requested_emotion=emotion,
            enhancement_status=status,
        )

    assert directory is not None
    audit_prompt, reference_language, transcription = await _reference_transcription(
        reference_path, request.prompt_text, target_language
    )
    supplied_prompt = (
        request.prompt_text.strip()
        if request.prompt_text and request.prompt_text.strip()
        else None
    )
    short_target = sum(character.isalnum() for character in request.text) <= 80
    cross_language = languages_differ(reference_language, target_language)
    languages_verified_same = bool(
        reference_language
        and target_language
        and normalize_language(reference_language) == normalize_language(target_language)
    )
    fallback = "none"
    if cross_language:
        effective_mode = TTSCloneMode.CONTROLLABLE
        if request.clone_mode == TTSCloneMode.ULTIMATE:
            fallback = "cross-language-ultimate-disabled"
    elif request.clone_mode == TTSCloneMode.AUTO:
        effective_mode = (
            TTSCloneMode.ULTIMATE
            if audit_prompt
            and languages_verified_same
            and (supplied_prompt is not None or not short_target)
            else TTSCloneMode.CONTROLLABLE
        )
        if (
            effective_mode == TTSCloneMode.CONTROLLABLE
            and languages_verified_same
            and short_target
            and supplied_prompt is None
        ):
            fallback = "short-auto-asr-reference-isolated"
    elif request.clone_mode == TTSCloneMode.ULTIMATE and not languages_verified_same:
        effective_mode = TTSCloneMode.CONTROLLABLE
        fallback = "language-unverified-ultimate-disabled"
    else:
        effective_mode = request.clone_mode

    selected_reference = await _select_reference_window(
        reference_path, transcription, directory
    )
    window_start: float | None = None
    window_duration: float | None = None
    window_metadata = directory / "reference-window.json"
    if window_metadata.is_file():
        try:
            metadata = json.loads(window_metadata.read_text(encoding="utf-8"))
            window_start = float(metadata["start_seconds"])
            window_duration = float(metadata["duration_seconds"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            window_start = None
    effective_strategy = request.emotion_strategy
    if effective_strategy == TTSEmotionStrategy.AUTO:
        requested = _canonical_emotion(request.emotion)
        detected = await _reference_emotion(selected_reference) if requested else None
        effective_strategy = (
            TTSEmotionStrategy.FORCE
            if requested and detected and requested != detected
            else TTSEmotionStrategy.INHERIT
        )

    enhanced_emotion = request.emotion
    enhancement_status = "disabled"
    style = ""
    if effective_strategy == TTSEmotionStrategy.FORCE:
        enhanced_emotion, enhancement_status = await enhance_emotion(
            get_settings(), request.text, request.emotion, request.emotion_enhance
        )
        style = build_style_instruction(
            enhanced_emotion, request.prosody, cloning=True
        )
    elif request.emotion_enhance:
        enhancement_status = "inherited"

    return CloneContext(
        original_reference_path=reference_path,
        # Ultimate continuation requires the reference audio and transcript to
        # cover exactly the same utterance.  The VAD-selected window does not
        # have a word-level aligned transcript, so keep the full reference for
        # Ultimate and reserve the clean window for isolated cloning/fallback.
        model_reference_path=(
            reference_path
            if effective_mode == TTSCloneMode.ULTIMATE
            else selected_reference
        ),
        isolated_reference_path=selected_reference,
        audit_prompt_text=audit_prompt,
        model_prompt_text=(
            audit_prompt if effective_mode == TTSCloneMode.ULTIMATE else None
        ),
        reference_language=reference_language,
        target_language=target_language,
        effective_mode=effective_mode,
        fallback=fallback,
        cross_language=cross_language,
        emotion_strategy=effective_strategy,
        style=style,
        # Inherit mode intentionally follows the reference performance.  Do
        # not fail every candidate against a textual target that was never
        # injected into the model; forced emotion remains fully gated.
        requested_emotion=(
            enhanced_emotion
            if effective_strategy == TTSEmotionStrategy.FORCE
            else None
        ),
        enhancement_status=enhancement_status,
        reference_window_start=(
            None if effective_mode == TTSCloneMode.ULTIMATE else window_start
        ),
        reference_window_duration=(
            None if effective_mode == TTSCloneMode.ULTIMATE else window_duration
        ),
    )


def _clone_headers(context: CloneContext, candidate_count: int) -> dict[str, str]:
    return {
        "X-TTS-Clone-Mode": (
            context.effective_mode.value if context.effective_mode else "none"
        ),
        "X-TTS-Clone-Fallback": context.fallback,
        "X-TTS-Reference-Language": context.reference_language or "unknown",
        "X-TTS-Target-Language": context.target_language or "unknown",
        "X-TTS-Emotion-Strategy": context.emotion_strategy.value,
        "X-TTS-Candidate-Count": str(candidate_count),
        "X-TTS-Reference-Window": (
            f"{context.reference_window_start:.3f}s+{context.reference_window_duration:.3f}s"
            if context.reference_window_start is not None
            and context.reference_window_duration is not None
            else "full"
        ),
    }


def _merge_clone_headers(
    response: Response,
    context: CloneContext,
    candidate_count: int,
) -> None:
    for name, value in _clone_headers(context, candidate_count).items():
        if name.lower() not in response.headers:
            response.headers[name] = value


@app.post(
    "/v2/tts/speech",
    tags=["语音合成"],
    summary="语音合成或 VoxCPM2 深度语音克隆",
    dependencies=[Depends(require_service_token)],
)
async def synthesize_v2(request: TTSPublicSpeechRequest) -> Response:
    settings = get_settings()
    if not settings.tts_enhanced_pipeline_enabled:
        common = request.model_dump(exclude={"reference_audio_url", "prompt_text"})
        if not request.reference_audio_url:
            return await _synthesize_v2_response(TTSSpeechRequest(**common))

    directory: Path | None = None
    reference_path: Path | None = None
    try:
        if request.reference_audio_url:
            directory = settings.tts_reference_dir / f"remote-{uuid4().hex}"
            media = await download_public_media_async(
                request.reference_audio_url,
                directory,
                "reference",
                AUDIO_MEDIA,
                MAX_UPLOAD_BYTES,
                settings.upstream_timeout_seconds,
            )
            reference_path = media.path
        context = await _prepare_clone_context(request, reference_path, directory)
        request_id = str(uuid4())
        if (
            request.quality_mode == TTSQualityMode.STRICT
            or _requires_synchronous_quality_gate(request, context)
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                request_id,
                always_three=False,
            )
            response.headers["X-TTS-Emotion-Enhancement"] = context.enhancement_status
            _merge_clone_headers(
                response,
                context,
                int(response.headers.get("x-tts-quality-attempts", "3")),
            )
            return response

        audio, provider, segment_count = await _synthesize_enhanced_once(
            request,
            context.model_reference_path,
            context.model_prompt_text,
            context.style,
            seed=request.seed if request.seed is not None else 42,
        )
        await _quality_store_put(
            request_id,
            {"request_id": request_id, "status": "pending"},
        )
        _run_background(
            _audit_and_store(
                request_id,
                request.text,
                request.language,
                audio,
                context.model_reference_path,
                context.audit_prompt_text,
                request.prosody.speed,
                context.requested_emotion,
                context.cross_language,
                directory,
            )
        )
        directory = None
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={**_clone_headers(context, 1),
                "X-TTS-Provider": provider,
                "X-TTS-Request-ID": request_id,
                "X-TTS-Quality-Status": "pending",
                "X-TTS-Quality-Attempts": "1",
                "X-TTS-Segment-Count": str(segment_count),
                "X-TTS-Emotion-Enhancement": context.enhancement_status,
            },
        )
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    finally:
        if directory is not None:
            await asyncio.to_thread(shutil.rmtree, directory, True)


async def _synthesize_enhanced_once(
    request: TTSPublicSpeechRequest,
    reference_path: Path | None,
    prompt_text: str | None,
    style: str,
    *,
    seed: int,
) -> tuple[bytes, str, int]:
    segment_limit = (
        20
        if reference_path is not None
        and prompt_text is None
        and infer_text_language(request.text, request.language) == "zh"
        else None
    )
    segments = split_tts_text(request.text, limit=segment_limit)
    semaphore = asyncio.Semaphore(2)

    async def generate(index: int) -> tuple[bytes, str]:
        segment = segments[index]
        for segment_attempt in range(3):
            common = request.model_dump(exclude={"reference_audio_url", "prompt_text"})
            common.pop("emotion", None)
            common.pop("emotion_enhance", None)
            common.pop("quality_mode", None)
            common.pop("clone_mode", None)
            common.pop("emotion_strategy", None)
            common.update(
                {
                    "text": model_text(
                        segment.text,
                        style,
                        cloning=reference_path is not None and not bool(style),
                    ),
                    "prosody": ProsodySpec(),
                    "seed": seed + segment_attempt,
                }
            )
            if reference_path is not None:
                internal: TTSSpeechRequest = TTSCloneSpeechRequest(
                    **{**common, "provider": TTSProviderName.VOXCPM2},
                    reference_audio_path=reference_path,
                    prompt_text=prompt_text,
                )
            else:
                internal = TTSSpeechRequest(**common)
            async with semaphore:
                response = await _synthesize_v2_response(internal)
            audio = bytes(response.body)
            try:
                metrics = wav_silence_metrics(audio)
                prepared = normalize_wav_silence(audio)
            except (EOFError, ValueError, wave.Error):
                return (
                    audio,
                    response.headers.get("x-tts-provider", "voxcpm2"),
                )
            if not metrics["internal_silence_anomaly"] or segment_attempt == 2:
                return (
                    prepared,
                    response.headers.get("x-tts-provider", "voxcpm2"),
                )
        raise AssertionError("unreachable segment retry state")

    generated = await asyncio.gather(*(generate(index) for index in range(len(segments))))
    audio = (
        generated[0][0]
        if len(generated) == 1
        else await asyncio.to_thread(
            combine_wav_segments,
            [item[0] for item in generated],
            segments,
        )
    )
    audio = await asyncio.to_thread(
        apply_prosody_wav,
        audio,
        get_settings().tts_ffmpeg_bin,
        request.prosody,
    )
    try:
        audio = await asyncio.to_thread(normalize_wav_silence, audio)
    except (EOFError, ValueError, wave.Error):
        pass
    return audio, generated[0][1], len(segments)


async def _stream_upstream_segment(
    client: httpx.AsyncClient,
    text: str,
    reference_path: Path | None,
    prompt_text: str | None,
    seed: int,
) -> AsyncIterator[bytes]:
    payload: dict[str, Any] = {
        "text": text,
        "cfg_value": 2.0,
        "inference_timesteps": 10,
        "seed": seed,
    }
    endpoint = "stream"
    if reference_path is not None:
        endpoint = "stream_path"
        payload["reference_wav_path"] = str(reference_path)
        if prompt_text:
            payload["prompt_text"] = prompt_text
    async with client.stream(
        "POST",
        f"{get_settings().tts_backend_url.rstrip('/')}/{endpoint}",
        json=payload,
    ) as response:
        if not response.is_success:
            await response.aread()
            raise RuntimeError(f"VoxCPM2 stream returned HTTP {response.status_code}")
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk


async def _collect_stream(source: AsyncIterator[bytes]) -> bytes:
    chunks = bytearray()
    async for chunk in source:
        chunks.extend(chunk)
    return bytes(chunks)


def _fade_pcm(data: bytes) -> bytes:
    samples = array("h")
    usable = len(data) - len(data) % PCM_SAMPLE_WIDTH
    samples.frombytes(data[:usable])
    frames = min(len(samples) // 2, round(PCM_SAMPLE_RATE * FADE_MS / 1000))
    for index in range(frames):
        samples[index] = round(samples[index] * index / max(1, frames))
        tail = len(samples) - index - 1
        samples[tail] = round(samples[tail] * index / max(1, frames))
    return samples.tobytes()


async def _fade_live_pcm(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    edge_bytes = round(PCM_SAMPLE_RATE * FADE_MS / 1000) * PCM_SAMPLE_WIDTH
    buffered = bytearray()
    first = True
    async for chunk in source:
        buffered.extend(chunk)
        if len(buffered) <= edge_bytes * 2:
            continue
        emit = bytes(buffered[:-edge_bytes])
        del buffered[:-edge_bytes]
        if first:
            prefix = _fade_pcm(emit[:edge_bytes] + b"\x00" * edge_bytes)[:edge_bytes]
            emit = prefix + emit[edge_bytes:]
            first = False
        yield emit
    if buffered:
        tail = _fade_pcm(b"\x00" * edge_bytes + bytes(buffered))[-len(buffered):]
        yield tail


async def _segmented_pcm_source(
    request: TTSPublicSpeechRequest,
    reference_path: Path | None,
    prompt_text: str | None,
    style: str,
) -> AsyncIterator[bytes]:
    segment_limit = (
        20
        if reference_path is not None
        and prompt_text is None
        and infer_text_language(request.text, request.language) == "zh"
        else None
    )
    segments = split_tts_text(request.text, limit=segment_limit)
    seed = request.seed if request.seed is not None else 42
    timeout = httpx.Timeout(get_settings().upstream_timeout_seconds, connect=15)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        prefetched: dict[int, asyncio.Task[bytes]] = {}

        def prefetch(index: int) -> None:
            if index < len(segments) and index not in prefetched:
                prefetched[index] = asyncio.create_task(
                    _collect_stream(
                        _stream_upstream_segment(
                            client,
                            model_text(
                                segments[index].text,
                                style,
                                cloning=reference_path is not None and not bool(style),
                            ),
                            reference_path,
                            prompt_text,
                            seed,
                        )
                    )
                )

        prefetch(1)
        prefetch(2)
        try:
            for index, segment in enumerate(segments):
                if index == 0:
                    current = _fade_live_pcm(
                        _stream_upstream_segment(
                            client,
                            model_text(
                                segment.text,
                                style,
                                cloning=reference_path is not None and not bool(style),
                            ),
                            reference_path,
                            prompt_text,
                            seed,
                        )
                    )
                    async for chunk in current:
                        yield chunk
                else:
                    data = _fade_pcm(await prefetched.pop(index))
                    for offset in range(0, len(data), 64 * 1024):
                        yield data[offset:offset + 64 * 1024]
                prefetch(index + 2)
                if index < len(segments) - 1:
                    pause = (
                        PARAGRAPH_SILENCE_MS
                        if segment.boundary == "paragraph"
                        else SENTENCE_SILENCE_MS
                    )
                    yield pcm_silence(pause)
        finally:
            for task in prefetched.values():
                if not task.done():
                    task.cancel()
            if prefetched:
                await asyncio.gather(*prefetched.values(), return_exceptions=True)


@app.post(
    "/v2/tts/speech/stream",
    tags=["语音合成"],
    summary="实时流式 VoxCPM2 语音合成",
    dependencies=[Depends(require_service_token)],
)
async def synthesize_v2_stream(request: TTSPublicSpeechRequest) -> StreamingResponse:
    if request.quality_mode == TTSQualityMode.STRICT:
        raise HTTPException(status_code=422, detail="strict quality mode is not available for streaming")
    settings = get_settings()
    directory = settings.tts_reference_dir / f"stream-{uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    reference_path: Path | None = None
    try:
        if request.reference_audio_url:
            media = await download_public_media_async(
                request.reference_audio_url,
                directory,
                "reference",
                AUDIO_MEDIA,
                MAX_UPLOAD_BYTES,
                settings.upstream_timeout_seconds,
            )
            reference_path = media.path
        context = await _prepare_clone_context(request, reference_path, directory)
        request_id = str(uuid4())
        await _quality_store_put(
            request_id,
            {"request_id": request_id, "status": "pending"},
        )
    except MediaFetchError as exc:
        await asyncio.to_thread(shutil.rmtree, directory, True)
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception:
        await asyncio.to_thread(shutil.rmtree, directory, True)
        raise

    async def body() -> AsyncIterator[bytes]:
        audit_path = directory / "audit.wav"
        completed = False
        source = _segmented_pcm_source(
            request,
            context.model_reference_path,
            context.model_prompt_text,
            context.style,
        )
        processed: AsyncIterator[bytes]
        if request.prosody == ProsodySpec():
            processed = source
        else:
            processed = process_pcm_stream(source, settings.tts_ffmpeg_bin, request.prosody)
        try:
            with wave.open(str(audit_path), "wb") as audit:
                audit.setnchannels(PCM_CHANNELS)
                audit.setsampwidth(PCM_SAMPLE_WIDTH)
                audit.setframerate(PCM_SAMPLE_RATE)
                async for chunk in processed:
                    audit.writeframesraw(chunk)
                    yield chunk
            completed = True
        finally:
            if completed:
                _run_background(
                    _audit_and_store(
                        request_id,
                        request.text,
                        request.language,
                        audit_path,
                        context.model_reference_path,
                        context.audit_prompt_text,
                        request.prosody.speed,
                        context.requested_emotion,
                        context.cross_language,
                        directory,
                    )
                )
            else:
                _run_background(
                    _quality_store_put(
                        request_id,
                        {"request_id": request_id, "status": "cancelled"},
                    )
                )
                shutil.rmtree(directory, ignore_errors=True)

    return StreamingResponse(
        body(),
        media_type="audio/pcm",
        headers={**_clone_headers(context, 1),
            "X-Audio-Sample-Rate": str(PCM_SAMPLE_RATE),
            "X-Audio-Channels": str(PCM_CHANNELS),
            "X-Audio-Sample-Format": "s16le",
            "X-TTS-Provider": "voxcpm2",
            "X-TTS-Request-ID": request_id,
            "X-TTS-Quality-Status": "pending",
            "X-TTS-Segment-Count": str(len(split_tts_text(request.text))),
            "X-TTS-Emotion-Enhancement": context.enhancement_status,
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.get(
    "/v2/tts/quality/{request_id}",
    tags=["语音合成"],
    summary="查询语音合成异步质量审计",
    dependencies=[Depends(require_service_token)],
)
async def get_tts_quality(request_id: UUID) -> dict[str, Any]:
    result = await asyncio.to_thread(get_tts_quality_store().get, str(request_id))
    if result is None:
        raise HTTPException(status_code=404, detail="TTS quality audit not found")
    return result


async def _resolve_prompt_text(reference_path: Path, prompt_text: str | None) -> str:
    if prompt_text and prompt_text.strip():
        return prompt_text.strip()
    content = await asyncio.to_thread(reference_path.read_bytes)
    transcription = await _transcribe_content(
        reference_path.name,
        content,
        None,
        5,
    )
    text = _transcription_text(transcription)
    if text:
        return text
    raise HTTPException(
        status_code=422,
        detail="ASR returned no text for the reference audio",
    )


def _transcription_text(transcription: dict[str, Any]) -> str:
    text = transcription.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    segments = transcription.get("segments")
    if isinstance(segments, list):
        joined = " ".join(
            segment["text"].strip()
            for segment in segments
            if isinstance(segment, dict)
            and isinstance(segment.get("text"), str)
            and segment["text"].strip()
        )
        if joined:
            return joined
    return ""


def _normalized_speech_text(value: str) -> str:
    digit_equivalents = str.maketrans("零〇一二三四五六七八九", "00123456789")
    orthographic_equivalents = str.maketrans(
        {
            "ё": "е",
            "Ё": "Е",
            "أ": "ا",
            "إ": "ا",
            "آ": "ا",
            "ٱ": "ا",
        }
    )
    return "".join(
        character.lower()
        for character in unicodedata.normalize("NFKC", value)
        .translate(digit_equivalents)
        .translate(orthographic_equivalents)
        if character.isalnum()
    )


def _quality_asr_language(text: str, requested_language: str) -> str | None:
    if requested_language != "auto":
        return requested_language
    if any("\u3040" <= character <= "\u30ff" for character in text):
        return "ja"
    if any("\uac00" <= character <= "\ud7af" for character in text):
        return "ko"
    if any("\u3400" <= character <= "\u9fff" for character in text):
        return "zh"
    return None


def _short_audio_duration_limit(text: str, requested_speed: float) -> float:
    units = len(_normalized_speech_text(text))
    return max(3.0, units * 0.55 + 1.5) / max(0.5, requested_speed)


def _requires_synchronous_quality_gate(
    request: TTSPublicSpeechRequest,
    context: CloneContext,
) -> bool:
    """Gate failure-prone short speech and every voice-clone response inline."""
    return bool(
        context.model_reference_path is not None
        or len(_normalized_speech_text(request.text)) <= 80
    )


def _wav_region_rms_dbfs(audio: bytes, start: float, end: float) -> float | None:
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            if source.getsampwidth() != 2 or source.getframerate() <= 0:
                return None
            rate = source.getframerate()
            channels = source.getnchannels()
            first = min(source.getnframes(), max(0, round(start * rate)))
            last = min(source.getnframes(), max(first, round(end * rate)))
            source.setpos(first)
            samples = array("h")
            samples.frombytes(source.readframes(last - first))
        if not samples or channels <= 0:
            return None
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        return 20.0 * math.log10(max(rms / 32768.0, 1e-8))
    except (EOFError, wave.Error):
        return None


def _tts_degraded_reasons(metrics: dict[str, Any]) -> list[str]:
    checks = (
        ("content", not bool(metrics.get("content_passed", True))),
        ("prefix", bool(metrics.get("unexpected_prefix"))),
        ("suffix", bool(metrics.get("unexpected_suffix"))),
        ("first-sound", bool(metrics.get("first_sound_anomaly"))),
        ("last-sound", bool(metrics.get("last_sound_anomaly"))),
        ("long-sound", bool(metrics.get("longest_sound_anomaly"))),
        ("duration", bool(metrics.get("duration_anomaly"))),
        ("active-tail", bool(metrics.get("energetic_tail"))),
        ("silent-tail", bool(metrics.get("silent_tail_anomaly"))),
        ("internal-silence", bool(metrics.get("internal_silence_anomaly"))),
        ("speaker", not bool(metrics.get("speaker_passed", True))),
        ("speed", not bool(metrics.get("speed_passed", True))),
        ("emotion", not bool(metrics.get("emotion_passed", True))),
    )
    return [name for name, failed in checks if failed]


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _phonetic_units(value: str) -> list[str]:
    normalized = _normalized_speech_text(value)
    if any("\u3040" <= character <= "\u30ff" for character in normalized):
        return list(
            "".join(
                item["hira"]
                for item in kakasi().convert(normalized)
            )
        )
    units: list[str] = []
    for character in normalized:
        if "\u3400" <= character <= "\u9fff":
            units.extend(
                lazy_pinyin(
                    character,
                    style=Style.NORMAL,
                )
            )
        else:
            units.append(character)
    return units


def _content_quality(
    expected: str,
    actual: str,
) -> tuple[int, int, float, int, float, bool]:
    left = _normalized_speech_text(expected)
    right = _normalized_speech_text(actual)
    edits = _edit_distance(list(left), list(right))
    phonetic_edits = _edit_distance(
        _phonetic_units(expected),
        _phonetic_units(actual),
    )
    # A one-character substitution is a complete semantic failure for short
    # prompts, while the same single ASR error is negligible in a 500-character
    # narration.  Keep homophone tolerance through phonetic_edits, but require
    # exact short-form content.
    allowed_edits = 0 if len(left) <= 20 else max(1, (len(left) + 99) // 100)
    error_rate = edits / max(1, len(left))
    phonetic_error_rate = phonetic_edits / max(1, len(left))
    return (
        edits,
        allowed_edits,
        error_rate,
        phonetic_edits,
        phonetic_error_rate,
        min(edits, phonetic_edits) <= allowed_edits,
    )


async def _quality_store_put(request_id: str, payload: dict[str, Any]) -> None:
    try:
        await asyncio.to_thread(get_tts_quality_store().put, request_id, payload)
    except Exception:
        return


def _canonical_emotion(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    groups = {
        "happy": (
            "欢快", "开心", "喜悦", "兴奋", "温暖", "真诚", "感染力",
            "happy", "joy", "warm",
        ),
        "sad": ("悲伤", "伤感", "难过", "sad"),
        "angry": ("愤怒", "生气", "严厉", "angry"),
        "fear": ("害怕", "恐惧", "紧张", "fear"),
        "surprise": ("惊讶", "惊喜", "surprise"),
        "neutral": ("平静", "中性", "自然", "neutral"),
    }
    return next(
        (name for name, words in groups.items() if any(word in lowered for word in words)),
        None,
    )


async def _emotion_quality(audio: bytes, requested: str | None) -> tuple[str | None, float | None]:
    canonical = _canonical_emotion(requested)
    if canonical is None:
        return None, None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10),
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{get_settings().emotion_verify_url.rstrip('/')}/v1/emotion",
                files={"audio": ("generated.wav", audio, "audio/wav")},
            )
        response.raise_for_status()
        body = response.json()
        scores = body.get("scores") or []
        aliases = {
            "happy": ("happy", "开心", "高兴", "喜悦"),
            "sad": ("sad", "悲伤", "难过"),
            "angry": ("angry", "生气", "愤怒"),
            "fear": ("fear", "恐惧", "害怕"),
            "surprise": ("surprise", "惊讶", "惊喜"),
            "neutral": ("neutral", "中性", "平静"),
        }[canonical]
        score = max(
            (
                float(item.get("score", 0))
                for item in scores
                if any(alias in str(item.get("label", "")).lower() for alias in aliases)
            ),
            default=None,
        )
        return str(body.get("label") or ""), score
    except (httpx.HTTPError, TypeError, ValueError):
        return None, None


async def _quality_metrics(
    expected_text: str,
    language: str,
    audio: bytes,
    reference_path: Path | None,
    prompt_text: str | None,
    requested_speed: float,
    requested_emotion: str | None,
    cross_language: bool = False,
    speaker_threshold: float | None = None,
) -> dict[str, Any]:
    transcription: dict[str, Any] = {}
    try:
        transcription = await _transcribe_content(
            "generated.wav",
            audio,
            _quality_asr_language(expected_text, language),
            5,
        )
        transcript = _transcription_text(transcription)
    except HTTPException:
        transcript = ""
    (
        edits,
        allowed_edits,
        content_cer,
        phonetic_edits,
        phonetic_cer,
        content_passed,
    ) = _content_quality(expected_text, transcript)
    words = [
        word
        for segment in transcription.get("segments") or []
        if isinstance(segment, dict)
        for word in segment.get("words") or []
        if isinstance(word, dict)
    ]
    durations = [
        float(word["end"]) - float(word["start"])
        for word in words
        if isinstance(word.get("start"), int | float)
        and isinstance(word.get("end"), int | float)
        and word["end"] > word["start"]
    ]
    median_duration = (
        sorted(durations)[len(durations) // 2] if durations else None
    )
    first_duration = durations[0] if durations else None
    last_duration = durations[-1] if durations else None
    last_baseline = (
        sorted(durations[:-1])[len(durations[:-1]) // 2]
        if len(durations) > 1
        else median_duration
    )
    first_probability = (
        float(words[0].get("probability", 0.0)) if words else None
    )
    last_probability = (
        float(words[-1].get("probability", 0.0)) if words else None
    )
    last_sound_stretched = bool(
        last_duration is not None
        and (
            (len(durations) == 1 and last_duration > 2.0)
            or (
                len(durations) > 1
                and last_baseline is not None
                and last_duration > 0.750
                and last_duration > last_baseline * 3.0
            )
        )
    )
    longest_sound_anomaly = bool(
        durations
        and median_duration is not None
        and max(durations) > 2.0
        and max(durations) > median_duration * 4.0
    )
    expected_normalized = _normalized_speech_text(expected_text)
    actual_normalized = _normalized_speech_text(transcript)
    expected_edge_units = _phonetic_units(expected_text)
    actual_edge_units = _phonetic_units(transcript)
    unexpected_prefix = bool(
        expected_edge_units
        and actual_edge_units
        and actual_edge_units[:1] != expected_edge_units[:1]
    )
    unexpected_suffix = bool(
        expected_edge_units
        and actual_edge_units[:len(expected_edge_units)] == expected_edge_units
        and len(actual_edge_units) > len(expected_edge_units)
    )
    first_sound_stretched = bool(
        first_duration is not None
        and median_duration is not None
        and first_duration > 0.350
        and first_duration > median_duration * 2.5
    )
    first_sound_severely_stretched = bool(
        first_duration is not None
        and median_duration is not None
        and first_duration > 1.0
        and first_duration > median_duration * 4.0
    )
    first_sound_anomaly = bool(
        first_sound_stretched
        and (
            first_sound_severely_stretched
            or first_probability is None
            or first_probability < 0.85
            or unexpected_prefix
            or not content_passed
        )
    )
    last_sound_anomaly = bool(
        last_sound_stretched
        and (
            (last_duration is not None and last_duration > 2.0)
            or last_probability is None
            or last_probability < 0.85
            or unexpected_suffix
            or not content_passed
        )
    )
    content_exact = bool(
        expected_normalized and actual_normalized == expected_normalized
    )
    speaker_similarity = (
        await _speaker_similarity(reference_path, audio)
        if reference_path is not None
        else None
    )
    try:
        duration = await asyncio.to_thread(wav_duration_seconds, audio)
    except Exception:
        duration = max(0.001, len(_normalized_speech_text(expected_text)) / 4.0)
    duration_limit = _short_audio_duration_limit(expected_text, requested_speed)
    duration_anomaly = duration > duration_limit
    last_word_end = (
        float(words[-1]["end"])
        if words and isinstance(words[-1].get("end"), int | float)
        else None
    )
    tail_seconds = (
        max(0.0, duration - last_word_end)
        if last_word_end is not None
        else 0.0
    )
    tail_rms_dbfs = (
        _wav_region_rms_dbfs(audio, max(last_word_end, duration - 0.25), duration)
        if last_word_end is not None and tail_seconds >= 0.25
        else None
    )
    energetic_tail = bool(
        tail_seconds > 0.50
        and tail_rms_dbfs is not None
        and tail_rms_dbfs > -45.0
    )
    try:
        silence_metrics = wav_silence_metrics(audio)
    except (EOFError, ValueError, wave.Error):
        silence_metrics = {
            "trailing_silence_seconds": 0.0,
            "max_internal_silence_seconds": 0.0,
            "silent_tail_anomaly": False,
            "internal_silence_anomaly": False,
        }
    silent_tail_anomaly = bool(silence_metrics["silent_tail_anomaly"])
    internal_silence_anomaly = bool(
        silence_metrics["internal_silence_anomaly"]
    )
    actual_rate = len(_normalized_speech_text(expected_text)) / max(duration, 0.001)
    target_rate: float | None = None
    if reference_path is not None and prompt_text and not cross_language:
        try:
            reference_bytes = await asyncio.to_thread(reference_path.read_bytes)
            reference_wav, reference_duration_ms = await asyncio.to_thread(
                normalize_to_wav,
                reference_bytes,
                get_settings().tts_ffmpeg_bin,
                PCM_SAMPLE_RATE,
                PCM_CHANNELS,
            )
            del reference_wav
            target_rate = (
                len(_normalized_speech_text(prompt_text))
                / max(reference_duration_ms / 1000, 0.001)
                * requested_speed
            )
        except Exception:
            target_rate = None
    speed_ratio = actual_rate / target_rate if target_rate else None
    speed_score = (
        max(0.0, 1.0 - abs(speed_ratio - 1.0))
        if speed_ratio is not None
        else None
    )
    emotion_label, emotion_score = await _emotion_quality(audio, requested_emotion)

    values: dict[str, tuple[float, float]] = {
        "content": (max(0.0, 1.0 - min(content_cer, phonetic_cer)), 0.45),
    }
    if speed_score is not None:
        values["speed"] = (speed_score, 0.15)
    if speaker_similarity is not None:
        values["speaker"] = (
            max(0.0, min(1.0, (speaker_similarity + 1.0) / 2.0)),
            0.25,
        )
    if emotion_score is not None:
        values["emotion"] = (max(0.0, min(1.0, emotion_score)), 0.15)
    weight = sum(item[1] for item in values.values())
    quality_score = sum(score * item_weight for score, item_weight in values.values()) / weight
    settings = get_settings()
    effective_speaker_threshold = (
        speaker_threshold
        if speaker_threshold is not None
        else settings.tts_speaker_similarity_threshold
    )
    speaker_passed = (
        reference_path is None
        or speaker_similarity is None
        or speaker_similarity >= effective_speaker_threshold
    )
    emotion_passed = (
        emotion_score is None or emotion_score >= settings.tts_emotion_score_threshold
    )
    # Reference characters/second is only a rough prosody signal: different
    # text naturally has a different phoneme mix. Keep a wider warning band
    # and do not regenerate otherwise correct audio solely for this estimate.
    speed_passed = target_rate is None or abs(speed_ratio - 1.0) <= 0.20
    quality_passed = (
        content_passed
        and speaker_passed
        and emotion_passed
        and not first_sound_anomaly
        and not last_sound_anomaly
        and not longest_sound_anomaly
        and not duration_anomaly
        and not energetic_tail
        and not silent_tail_anomaly
        and not internal_silence_anomaly
        and not unexpected_prefix
        and not unexpected_suffix
    )
    return {
        "status": "completed",
        "content_edits": edits,
        "content_allowed_edits": allowed_edits,
        "content_cer": round(content_cer, 6),
        "content_phonetic_edits": phonetic_edits,
        "content_phonetic_cer": round(phonetic_cer, 6),
        "content_passed": content_passed,
        "content_exact": content_exact,
        "speaker_similarity": (
            round(speaker_similarity, 6) if speaker_similarity is not None else None
        ),
        "actual_characters_per_second": round(actual_rate, 6),
        "target_characters_per_second": (
            round(target_rate, 6) if target_rate is not None else None
        ),
        "speed_ratio": round(speed_ratio, 6) if speed_ratio is not None else None,
        "speed_score": round(speed_score, 6) if speed_score is not None else None,
        "emotion_label": emotion_label,
        "emotion_score": round(emotion_score, 6) if emotion_score is not None else None,
        "first_sound_duration_seconds": (
            round(first_duration, 6) if first_duration is not None else None
        ),
        "first_sound_probability": (
            round(first_probability, 6) if first_probability is not None else None
        ),
        "last_sound_duration_seconds": (
            round(last_duration, 6) if last_duration is not None else None
        ),
        "last_sound_probability": (
            round(last_probability, 6) if last_probability is not None else None
        ),
        "longest_sound_duration_seconds": (
            round(max(durations), 6) if durations else None
        ),
        "first_sound_anomaly": first_sound_anomaly,
        "last_sound_anomaly": last_sound_anomaly,
        "longest_sound_anomaly": longest_sound_anomaly,
        "unexpected_prefix": unexpected_prefix,
        "unexpected_suffix": unexpected_suffix,
        "duration_seconds": round(duration, 6),
        "duration_limit_seconds": round(duration_limit, 6),
        "duration_anomaly": duration_anomaly,
        "last_word_end_seconds": (
            round(last_word_end, 6) if last_word_end is not None else None
        ),
        "tail_seconds": round(tail_seconds, 6),
        "tail_rms_dbfs": (
            round(tail_rms_dbfs, 6) if tail_rms_dbfs is not None else None
        ),
        "energetic_tail": energetic_tail,
        "trailing_silence_seconds": silence_metrics["trailing_silence_seconds"],
        "max_internal_silence_seconds": silence_metrics[
            "max_internal_silence_seconds"
        ],
        "silent_tail_anomaly": silent_tail_anomaly,
        "internal_silence_anomaly": internal_silence_anomaly,
        "speaker_passed": speaker_passed,
        "speed_passed": speed_passed,
        "emotion_passed": emotion_passed,
        "cross_language": cross_language,
        "quality_score": round(quality_score, 6),
        "quality_passed": quality_passed,
    }


async def _audit_and_store(
    request_id: str,
    expected_text: str,
    language: str,
    audio: bytes | Path,
    reference_path: Path | None,
    prompt_text: str | None,
    requested_speed: float,
    requested_emotion: str | None,
    cross_language: bool,
    cleanup_dir: Path | None,
) -> None:
    try:
        audio_bytes = (
            await asyncio.to_thread(audio.read_bytes)
            if isinstance(audio, Path)
            else audio
        )
        metrics = await _quality_metrics(
            expected_text,
            language,
            audio_bytes,
            reference_path,
            prompt_text,
            requested_speed,
            requested_emotion,
            cross_language,
        )
        await _quality_store_put(request_id, {"request_id": request_id, **metrics})
    except Exception as exc:
        await _quality_store_put(
            request_id,
            {"request_id": request_id, "status": "failed", "error": type(exc).__name__},
        )
    finally:
        if cleanup_dir is not None:
            await asyncio.to_thread(shutil.rmtree, cleanup_dir, True)


async def _strict_enhanced_response(
    request: TTSPublicSpeechRequest,
    context: CloneContext,
    request_id: str,
    *,
    parallel: bool = False,
    always_three: bool = False,
) -> Response:
    best: tuple[tuple[Any, ...], bytes, dict[str, Any], str, int, int] | None = None
    normalized_units = len(_normalized_speech_text(request.text))
    # VoxCPM2's 1-3 character path frequently produces no audio for its usual
    # seed 42.  Seed 45 is stable in the pinned backend and still gives three
    # distinct deterministic candidates (45, 46, 47).  Explicit caller seeds
    # remain authoritative.
    base_seed = (
        request.seed
        if request.seed is not None
        else 45
        if normalized_units <= 3
        or (context.target_language == "ja" and normalized_units <= 20)
        else 43 if context.cross_language
        else 42
    )
    speaker_threshold = (
        0.31
        if context.emotion_strategy == TTSEmotionStrategy.FORCE
        # Cross-language ERes2NetV2 scores are lower than same-language scores
        # Spanish-to-Chinese reference even when blind review confirms the
        # identity.  0.55 preserves a meaningful cross-language voice gate
        # without forcing three known-equivalent candidates every request.
        else max(0.55, get_settings().tts_speaker_similarity_threshold)
        if context.cross_language
        else None
    )

    async def generate_and_score(attempt: int):
        if context.cross_language and request.seed is None:
            # The pinned VoxCPM2 backend has complementary clean candidates at
            # seeds 52/53 for language pairs whose first four seeds preserve
            # content but miss the ERes2NetV2 threshold. They are only reached
            # after the normal candidates fail, so healthy requests stay fast.
            seed = (43, 44, 45, 46, 52, 53)[attempt - 1]
        else:
            seed = base_seed + attempt - 1
        audio, provider, segment_count = await _synthesize_enhanced_once(
            request,
            context.model_reference_path,
            context.model_prompt_text,
            context.style,
            seed=seed,
        )
        metrics = await _quality_metrics(
            request.text,
            request.language,
            audio,
            context.model_reference_path,
            context.audit_prompt_text,
            request.prosody.speed,
            context.requested_emotion,
            context.cross_language,
            speaker_threshold,
        )
        return attempt, audio, provider, segment_count, metrics

    last_generation_error: TTSProviderError | None = None
    attempts_executed = 0
    isolated_fallback_attempt: int | None = None
    if parallel:
        attempts_executed = 3
        gathered = await asyncio.gather(
            *(generate_and_score(attempt) for attempt in range(1, 4)),
            return_exceptions=True,
        )
        results = []
        for item in gathered:
            if isinstance(item, TTSProviderError):
                last_generation_error = item
            elif isinstance(item, BaseException):
                raise item
            else:
                results.append(item)
    else:
        results = []
        max_attempts = 3 if always_three or not context.cross_language else 6
        for attempt in range(1, max_attempts + 1):
            attempts_executed = attempt
            try:
                result = await generate_and_score(attempt)
            except TTSProviderError as exc:
                last_generation_error = exc
                continue
            results.append(result)
            if result[4]["quality_passed"] and not always_three:
                break

    primary_passed = any(bool(item[4].get("quality_passed")) for item in results)
    if (
        not primary_passed
        and context.effective_mode == TTSCloneMode.ULTIMATE
        and context.isolated_reference_path is not None
    ):
        isolated_fallback_attempt = 4
        attempts_executed = max(attempts_executed, isolated_fallback_attempt)
        try:
            fallback_audio, fallback_provider, fallback_segments = (
                await _synthesize_enhanced_once(
                    request,
                    context.isolated_reference_path,
                    None,
                    context.style,
                    seed=base_seed + isolated_fallback_attempt - 1,
                )
            )
            fallback_metrics = await _quality_metrics(
                request.text,
                request.language,
                fallback_audio,
                context.original_reference_path or context.isolated_reference_path,
                context.audit_prompt_text,
                request.prosody.speed,
                context.requested_emotion,
                False,
                None,
            )
            results.append(
                (
                    isolated_fallback_attempt,
                    fallback_audio,
                    fallback_provider,
                    fallback_segments,
                    fallback_metrics,
                )
            )
        except TTSProviderError as exc:
            last_generation_error = exc

    if not results:
        if last_generation_error is not None:
            raise last_generation_error
        raise TransientTTSProviderError("VoxCPM2 produced no usable audio candidate")

    for attempt, audio, provider, segment_count, metrics in results:
        content_passed = bool(metrics.get("content_passed")) or min(
            int(metrics["content_edits"]),
            int(metrics["content_phonetic_edits"]),
        ) <= int(metrics["content_allowed_edits"])
        clean_edges = not any(
            bool(metrics.get(name))
            for name in (
                "first_sound_anomaly",
                "last_sound_anomaly",
                "longest_sound_anomaly",
                "duration_anomaly",
                "energetic_tail",
                "silent_tail_anomaly",
                "internal_silence_anomaly",
                "unexpected_prefix",
                "unexpected_suffix",
            )
        )
        duration = metrics.get("duration_seconds")
        duration_rank = (
            -float(duration)
            if isinstance(duration, int | float)
            else float("-inf")
        )
        speed_score = metrics.get("speed_score")
        speed_rank = (
            float(speed_score)
            if request.prosody.speed != 1.0
            and isinstance(speed_score, int | float)
            else -1.0
            if request.prosody.speed != 1.0
            else 0.0
        )
        rank: tuple[Any, ...] = (
            bool(metrics["quality_passed"]),
            content_passed,
            clean_edges,
            speed_rank,
            float(metrics.get("speaker_similarity") or -1.0),
            duration_rank,
            float(metrics["quality_score"]),
        )
        candidate = (
            rank,
            audio,
            metrics,
            provider,
            segment_count,
            attempt,
        )
        if best is None or candidate[0] > best[0]:
            best = candidate
    assert best is not None
    _, audio, metrics, provider, segment_count, selected_attempt = best
    isolated_fallback_selected = selected_attempt == isolated_fallback_attempt
    selected_reference_path = (
        context.isolated_reference_path
        if isolated_fallback_selected
        else context.model_reference_path
    )
    selected_quality_reference_path = (
        (context.original_reference_path or selected_reference_path)
        if isolated_fallback_selected
        else selected_reference_path
    )
    selected_prompt_text = context.audit_prompt_text
    speed_calibrated = False
    speed_correction = 1.0
    selected_speed_ratio = metrics.get("speed_ratio")
    if (
        request.prosody.speed != 1.0
        and isinstance(selected_speed_ratio, int | float)
        and abs(float(selected_speed_ratio) - 1.0) > 0.05
    ):
        speed_correction = max(
            0.8,
            min(1.35, 1.0 / max(float(selected_speed_ratio), 0.001)),
        )
        if abs(speed_correction - 1.0) >= 0.02:
            audio = await asyncio.to_thread(
                apply_prosody_wav,
                audio,
                get_settings().tts_ffmpeg_bin,
                ProsodySpec(speed=speed_correction),
            )
            metrics = await _quality_metrics(
                request.text,
                request.language,
                audio,
                selected_quality_reference_path,
                selected_prompt_text,
                request.prosody.speed,
                context.requested_emotion,
                context.cross_language,
                speaker_threshold,
            )
            speed_calibrated = True
    tail_trimmed = False
    last_word_end = metrics.get("last_word_end_seconds")
    if (
        not metrics["quality_passed"]
        and metrics.get("content_exact")
        and (
            metrics.get("energetic_tail")
            or metrics.get("silent_tail_anomaly")
        )
        and isinstance(last_word_end, int | float)
    ):
        trimmed = await asyncio.to_thread(
            trim_wav_end,
            audio,
            float(last_word_end) + 0.25,
        )
        trimmed_metrics = await _quality_metrics(
            request.text,
            request.language,
            trimmed,
            selected_quality_reference_path,
            selected_prompt_text,
            request.prosody.speed,
            context.requested_emotion,
            context.cross_language,
            speaker_threshold,
        )
        if bool(trimmed_metrics.get("content_passed")) and not bool(
            trimmed_metrics.get("energetic_tail")
        ) and not bool(trimmed_metrics.get("silent_tail_anomaly")):
            audio = trimmed
            metrics = trimmed_metrics
            tail_trimmed = True
    degraded_reasons = _tts_degraded_reasons(metrics)
    await _quality_store_put(request_id, {"request_id": request_id, **metrics})
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "X-TTS-Provider": provider,
            "X-TTS-Request-ID": request_id,
            "X-TTS-Quality-Status": "completed",
            "X-TTS-Quality-Attempts": str(attempts_executed),
            "X-TTS-Selected-Attempt": str(selected_attempt),
            "X-TTS-Segment-Count": str(segment_count),
            "X-TTS-Content-CER": f"{metrics['content_cer']:.4f}",
            "X-TTS-Content-Phonetic-CER": f"{metrics['content_phonetic_cer']:.4f}",
            "X-TTS-Speaker-Similarity": (
                f"{metrics['speaker_similarity']:.4f}"
                if metrics["speaker_similarity"] is not None
                else "unavailable"
            ),
            "X-TTS-Speed-Ratio": (
                f"{float(metrics['speed_ratio']):.4f}"
                if metrics.get("speed_ratio") is not None
                else "unavailable"
            ),
            "X-TTS-Requested-Speed": f"{request.prosody.speed:.4f}",
            "X-TTS-Speed-Calibrated": str(speed_calibrated).lower(),
            "X-TTS-Speed-Correction": f"{speed_correction:.4f}",
            "X-TTS-First-Sound-Duration": (
                f"{metrics['first_sound_duration_seconds']:.4f}"
                if metrics.get("first_sound_duration_seconds") is not None
                else "unavailable"
            ),
            "X-TTS-Last-Sound-Duration": (
                f"{metrics['last_sound_duration_seconds']:.4f}"
                if metrics.get("last_sound_duration_seconds") is not None
                else "unavailable"
            ),
            "X-TTS-Audio-Duration": (
                f"{metrics['duration_seconds']:.4f}"
                if metrics.get("duration_seconds") is not None
                else "unavailable"
            ),
            "X-TTS-Unexpected-Prefix": str(
                bool(metrics.get("unexpected_prefix"))
            ).lower(),
            "X-TTS-Unexpected-Suffix": str(
                bool(metrics.get("unexpected_suffix"))
            ).lower(),
            "X-TTS-Emotion-Score": (
                f"{metrics['emotion_score']:.4f}"
                if metrics["emotion_score"] is not None
                else "unavailable"
            ),
            "X-TTS-Quality-Passed": str(metrics["quality_passed"]).lower(),
            "X-TTS-Degraded-Reason": (
                ",".join(degraded_reasons) if degraded_reasons else "none"
            ),
            "X-TTS-Tail-Trimmed": str(tail_trimmed).lower(),
            "X-TTS-Silent-Tail": str(
                bool(metrics.get("silent_tail_anomaly"))
            ).lower(),
            "X-TTS-Max-Internal-Silence": (
                f"{float(metrics.get('max_internal_silence_seconds') or 0.0):.4f}"
            ),
            "X-TTS-Reference-Fallback": (
                "ultimate-quality-failed-controllable"
                if isolated_fallback_selected
                else "none"
            ),
            "X-TTS-Clone-Fallback": (
                "ultimate-quality-failed-controllable"
                if isolated_fallback_selected
                else context.fallback
            ),
            "X-TTS-Clone-Mode": (
                TTSCloneMode.CONTROLLABLE.value
                if isolated_fallback_selected
                else context.effective_mode.value
                if context.effective_mode is not None
                else "none"
            ),
            "X-TTS-Candidate-Similarities": ",".join(
                "unavailable"
                if item[4].get("speaker_similarity") is None
                else f"{float(item[4]['speaker_similarity']):.4f}"
                for item in results
            ),
            "X-TTS-Candidate-Speed-Ratios": ",".join(
                "unavailable"
                if item[4].get("speed_ratio") is None
                else f"{float(item[4]['speed_ratio']):.4f}"
                for item in results
            ),
        },
    )


async def _clone_prompt_text(
    reference_path: Path,
    prompt_text: str | None,
) -> str:
    return await _resolve_prompt_text(reference_path, prompt_text)


async def _speaker_similarity(
    reference_path: Path,
    candidate_audio: bytes,
) -> float | None:
    settings = get_settings()
    try:
        reference_audio = await asyncio.to_thread(reference_path.read_bytes)
        cache_key = hashlib.sha256(reference_audio).hexdigest() + ":" + hashlib.sha256(
            candidate_audio
        ).hexdigest()
        now = time.monotonic()
        cached = _SPEAKER_SIMILARITY_CACHE.get(cache_key)
        if cached is not None and now - cached[0] <= REFERENCE_EMOTION_CACHE_TTL_SECONDS:
            return cached[1]
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10),
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{settings.speaker_verify_url.rstrip('/')}/v1/speaker-similarity",
                files={
                    "reference": (
                        reference_path.name,
                        reference_audio,
                        "application/octet-stream",
                    ),
                    "candidate": ("generated.wav", candidate_audio, "audio/wav"),
                },
            )
        response.raise_for_status()
        similarity = response.json().get("similarity")
        if not isinstance(similarity, int | float):
            return None
        result = float(similarity)
        _SPEAKER_SIMILARITY_CACHE[cache_key] = (now, result)
        if len(_SPEAKER_SIMILARITY_CACHE) > SPEAKER_SIMILARITY_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _SPEAKER_SIMILARITY_CACHE,
                key=lambda key: _SPEAKER_SIMILARITY_CACHE[key][0],
            )
            _SPEAKER_SIMILARITY_CACHE.pop(oldest_key, None)
        return result
    except (OSError, ValueError, httpx.HTTPError):
        return None


async def _synthesize_v2_response(request: TTSSpeechRequest) -> Response:
    try:
        result = await get_tts_service().synthesize(request)
    except VoiceProfileNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=f"voice profile not found: {exc.args[0]}",
        ) from exc
    except PermanentTTSProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TransientTTSProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TTSProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    headers = {
        "X-TTS-Provider": result.provider,
        "X-Audio-Duration-Ms": str(result.audio_duration_ms),
    }
    if result.provider_request_id:
        headers["X-Provider-Request-Id"] = result.provider_request_id
    return Response(
        content=result.audio,
        media_type="audio/wav",
        headers=headers,
    )


async def _synthesize_clone_response(request: TTSCloneSpeechRequest) -> Response:
    settings = get_settings()
    best: tuple[
        tuple[bool, bool, float],
        Response,
        int,
        int,
        int,
        float,
        int,
        float,
        float | None,
        bool,
    ] | None = None
    for attempt in range(1, 4):
        response = await _synthesize_v2_response(request)
        audio = bytes(response.body)
        try:
            transcription = await _transcribe_content(
                "generated.wav",
                audio,
                _quality_asr_language(request.text, request.language),
                5,
            )
            transcript = _transcription_text(transcription)
        except HTTPException:
            transcript = ""
        (
            edits,
            allowed_edits,
            error_rate,
            phonetic_edits,
            phonetic_error_rate,
            content_passed,
        ) = _content_quality(request.text, transcript)
        similarity = await _speaker_similarity(request.reference_audio_path, audio)
        speaker_passed = (
            similarity is not None
            and similarity >= settings.tts_speaker_similarity_threshold
        )
        normalized_similarity = (
            max(0.0, min(1.0, (similarity + 1.0) / 2.0))
            if similarity is not None
            else 0.0
        )
        content_accuracy = max(
            0.0,
            1.0 - min(error_rate, phonetic_error_rate),
        )
        quality_score = 0.65 * content_accuracy + 0.35 * normalized_similarity
        quality_passed = content_passed and speaker_passed
        rank = (quality_passed, content_passed, quality_score)
        candidate = (
            rank,
            response,
            attempt,
            edits,
            allowed_edits,
            error_rate,
            phonetic_edits,
            phonetic_error_rate,
            similarity,
            quality_passed,
        )
        if best is None or candidate[0] > best[0]:
            best = candidate
        if quality_passed:
            break

    assert best is not None
    (
        _,
        selected,
        selected_attempt,
        edits,
        allowed_edits,
        error_rate,
        phonetic_edits,
        phonetic_error_rate,
        similarity,
        quality_passed,
    ) = best
    selected.headers["X-TTS-Quality-Attempts"] = str(attempt)
    selected.headers["X-TTS-Selected-Attempt"] = str(selected_attempt)
    selected.headers["X-TTS-Content-Edits"] = str(edits)
    selected.headers["X-TTS-Content-Allowed-Edits"] = str(allowed_edits)
    selected.headers["X-TTS-Content-CER"] = f"{error_rate:.4f}"
    selected.headers["X-TTS-Content-Phonetic-Edits"] = str(phonetic_edits)
    selected.headers["X-TTS-Content-Phonetic-CER"] = f"{phonetic_error_rate:.4f}"
    selected.headers["X-TTS-Speaker-Similarity"] = (
        f"{similarity:.4f}" if similarity is not None else "unavailable"
    )
    selected.headers["X-TTS-Quality-Passed"] = str(quality_passed).lower()
    return selected


async def _save_reference_audio(
    upload: UploadFile,
    target_dir: Path,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    upload_id = uuid4().hex
    temporary = target_dir / f".upload-{upload_id}.part"
    size = 0
    signature = bytearray()
    try:
        with temporary.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail="reference audio is too large")
                if len(signature) < 64:
                    signature.extend(chunk[: 64 - len(signature)])
                output.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="empty reference audio file")
        suffix = sniff_media_suffix(bytes(signature))
        if suffix not in TTS_REFERENCE_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail="uploaded file header is not a supported audio format",
            )
        target = target_dir / f"upload-{upload_id}{suffix}"
        temporary.replace(target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@app.post(
    "/v2/tts/speech/upload",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def synthesize_v2_upload(
    text: str = Form(min_length=1, max_length=5000),
    language: str = Form(default="auto", min_length=2, max_length=16),
    voice_profile_id: str = Form(default="default", min_length=1, max_length=128),
    provider: TTSProviderName = Form(default=TTSProviderName.AUTO),
    reference_audio: UploadFile | None = File(default=None),
    prompt_text: str | None = Form(default=None, max_length=5000),
    speed: float = Form(default=1.0, ge=0.5, le=2.0),
    volume: float = Form(default=1.0, ge=0.1, le=2.0),
    pitch: float = Form(default=1.0, ge=0.5, le=2.0),
    emotion: str | None = Form(default=None, max_length=200),
    emotion_enhance: bool = Form(default=False),
    quality_mode: TTSQualityMode = Form(default=TTSQualityMode.STANDARD),
    clone_mode: TTSCloneMode = Form(default=TTSCloneMode.AUTO),
    emotion_strategy: TTSEmotionStrategy = Form(default=TTSEmotionStrategy.AUTO),
) -> Response:
    common = {
        "text": text,
        "language": language,
        "voice_profile_id": voice_profile_id,
        "provider": provider,
        "audio": AudioSpec(),
        "prosody": ProsodySpec(speed=speed, volume=volume, pitch=pitch),
        "timing": TimingSpec(),
        "emotion": emotion,
        "emotion_enhance": emotion_enhance,
        "quality_mode": quality_mode,
        "clone_mode": clone_mode,
        "emotion_strategy": emotion_strategy,
    }
    request = TTSPublicSpeechRequest(**common)
    settings = get_settings()
    directory: Path | None = None
    reference_path: Path | None = None
    try:
        if reference_audio is not None:
            directory = settings.tts_reference_dir / f"upload-{uuid4().hex}"
            reference_path = await _save_reference_audio(reference_audio, directory)
        context = await _prepare_clone_context(request, reference_path, directory)
        request_id = str(uuid4())
        if (
            quality_mode == TTSQualityMode.STRICT
            or _requires_synchronous_quality_gate(request, context)
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                request_id,
                always_three=False,
            )
            response.headers["X-TTS-Emotion-Enhancement"] = context.enhancement_status
            _merge_clone_headers(
                response,
                context,
                int(response.headers.get("x-tts-quality-attempts", "3")),
            )
            return response
        audio, selected_provider, segment_count = await _synthesize_enhanced_once(
            request,
            context.model_reference_path,
            context.model_prompt_text,
            context.style,
            seed=42,
        )
        await _quality_store_put(request_id, {"request_id": request_id, "status": "pending"})
        _run_background(
            _audit_and_store(
                request_id,
                text,
                language,
                audio,
                context.model_reference_path,
                context.audit_prompt_text,
                speed,
                context.requested_emotion,
                context.cross_language,
                directory,
            )
        )
        directory = None
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={**_clone_headers(context, 1),
                "X-TTS-Provider": selected_provider,
                "X-TTS-Request-ID": request_id,
                "X-TTS-Quality-Status": "pending",
                "X-TTS-Quality-Attempts": "1",
                "X-TTS-Segment-Count": str(segment_count),
                "X-TTS-Emotion-Enhancement": context.enhancement_status,
            },
        )
    finally:
        if directory is not None:
            await asyncio.to_thread(shutil.rmtree, directory, True)


@app.post(
    "/v2/tts/jobs",
    tags=["语音合成"],
    summary="创建异步语音合成任务",
    response_model=TTSJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_tts_job(request: TTSJobRequest) -> TTSJobAccepted:
    if request.reference_audio_url:
        try:
            await asyncio.to_thread(
                validate_public_https_url,
                request.reference_audio_url,
            )
        except MediaFetchError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
    try:
        return await asyncio.to_thread(get_tts_jobs().submit, request)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue TTS job: {type(exc).__name__}",
        ) from exc


@app.get(
    "/v2/tts/jobs/{job_id}",
    tags=["语音合成"],
    summary="查询异步语音合成任务状态",
    response_model=TTSJobStatus,
    dependencies=[Depends(require_service_token)],
)
async def get_tts_job(job_id: str) -> TTSJobStatus:
    try:
        return await asyncio.to_thread(get_tts_jobs().status, job_id)
    except TTSJobNotFound as exc:
        raise HTTPException(status_code=404, detail="TTS job not found") from exc


@app.get(
    "/v2/tts/jobs/{job_id}/audio",
    tags=["语音合成"],
    summary="下载异步语音合成结果",
    dependencies=[Depends(require_service_token)],
)
async def get_tts_job_audio(job_id: str) -> FileResponse:
    try:
        path = await asyncio.to_thread(get_tts_jobs().audio_path, job_id)
    except TTSJobNotFound as exc:
        raise HTTPException(status_code=404, detail="TTS job not found") from exc
    except TTSJobNotReady as exc:
        raise HTTPException(status_code=409, detail="TTS job is not complete") from exc
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"{job_id}.wav",
    )


@app.get(
    "/v2/tts/providers",
    tags=["语音合成"],
    summary="查询语音合成服务商状态",
    dependencies=[Depends(require_service_token)],
)
async def tts_provider_statuses() -> dict[str, Any]:
    return {"providers": get_tts_service().statuses()}


@app.get(
    "/v2/tts/voices",
    tags=["语音合成"],
    summary="查询可用音色档案",
    dependencies=[Depends(require_service_token)],
)
async def list_tts_voices() -> dict[str, Any]:
    return {
        "voices": [
            profile.model_dump(mode="json")
            for profile in get_tts_service().registry.list()
        ]
    }


@app.put(
    "/v2/tts/voices/{voice_profile_id}",
    include_in_schema=False,
    response_model=VoiceProfile,
    dependencies=[Depends(require_service_token)],
)
async def put_tts_voice(
    voice_profile_id: str,
    profile: VoiceProfile,
) -> VoiceProfile:
    if voice_profile_id != profile.voice_profile_id:
        raise HTTPException(
            status_code=400,
            detail="voice_profile_id path and body values must match",
        )
    return await asyncio.to_thread(get_tts_service().registry.put, profile)


@app.get(
    "/v1/admin/gpus",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def gpu_states() -> dict[str, Any]:
    return {"gpus": await asyncio.to_thread(get_gpu_controller().all_states)}


@app.post(
    "/v1/admin/gpus/{gpu_id}/drain",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def drain_gpu(gpu_id: int) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_gpu_controller().drain, gpu_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/v1/admin/gpus/{gpu_id}/disable",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def disable_gpu(gpu_id: int) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_gpu_controller().drain, gpu_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/v1/admin/gpus/{gpu_id}/enable",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def enable_gpu(gpu_id: int) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_gpu_controller().enable, gpu_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _validate_generation_request(request: VideoGenerationRequest) -> None:
    try:
        for value in (*request.reference_image_urls,*request.reference_video_urls,*request.reference_audio_urls):
            await asyncio.to_thread(validate_public_https_url,value)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code,exc.detail) from exc
    store=get_capability_store()
    candidates=[request.channel] if request.channel!="auto" else ["jmapi","libtv"]
    compatible=False
    for code in candidates:
        try: binding=await asyncio.to_thread(store.binding,request.model,code)
        except CapabilityNotFound: continue
        fits=generation_request_compatible(binding,request.model_dump())
        if fits and binding["enabled"] and binding["channel_enabled"] and binding["health_status"]=="online": compatible=True; break
    if not compatible:
        raise HTTPException(422,"no enabled, healthy channel supports this model and input combination")


async def _validate_image_generation_request(request:ImageGenerationRequest)->None:
    try:
        for value in request.reference_image_urls: await asyncio.to_thread(validate_public_https_url,value)
    except MediaFetchError as exc: raise HTTPException(exc.status_code,exc.detail) from exc
    try: binding=await asyncio.to_thread(get_capability_store().binding,request.model,request.channel)
    except CapabilityNotFound as exc: raise HTTPException(404,"model or channel not found") from exc
    if not binding["enabled"] or not binding["channel_enabled"] or binding["health_status"]!="online": raise HTTPException(503,"image generation channel is unavailable")
    if len(request.reference_image_urls)>binding["capabilities"].get("images",0): raise HTTPException(422,"too many reference images")


@app.post("/v1/video-generations/jobs",tags=["通用视频生成"],summary="创建原子视频生成任务",status_code=202,dependencies=[Depends(require_service_token)])
async def create_video_generation_job(request:VideoGenerationRequest)->dict[str,Any]:
    await _validate_generation_request(request)
    try: jid=await asyncio.to_thread(get_generation_jobs().submit,request.model_dump())
    except CapabilityNotFound as exc: raise HTTPException(404,"model not found") from exc
    except Exception as exc: raise HTTPException(503,"unable to enqueue video generation job") from exc
    return {"job_id":jid,"status":"queued","model":request.model,"requested_channel":request.channel,"status_url":f"/v1/video-generations/jobs/{jid}"}


@app.get("/v1/video-generations/jobs/{job_id}",tags=["通用视频生成"],summary="查询原子视频生成任务",dependencies=[Depends(require_service_token)])
async def get_video_generation_job(job_id:UUID)->dict[str,Any]:
    try:return await asyncio.to_thread(get_generation_jobs().status,str(job_id))
    except CapabilityNotFound as exc:raise HTTPException(404,"generation job not found") from exc


@app.post("/v1/video-generations/jobs/{job_id}/cancel",tags=["通用视频生成"],summary="取消原子视频生成任务",dependencies=[Depends(require_service_token)])
async def cancel_video_generation_job(job_id:UUID)->dict[str,Any]:
    try:return await asyncio.to_thread(get_generation_jobs().cancel,str(job_id))
    except CapabilityNotFound as exc:raise HTTPException(404,"generation job not found") from exc


@app.post("/v1/image-generations/jobs",tags=["图像生成"],summary="创建原子图像生成任务",status_code=202,dependencies=[Depends(require_service_token)])
async def create_image_generation_job(request:ImageGenerationRequest)->dict[str,Any]:
    await _validate_image_generation_request(request)
    data=request.model_dump(); data["reference_video_urls"]=[]; data["reference_audio_urls"]=[]
    try: jid=await asyncio.to_thread(get_generation_jobs().submit,data)
    except Exception as exc: raise HTTPException(503,"unable to enqueue image generation job") from exc
    return {"job_id":jid,"status":"queued","model":request.model,"requested_channel":request.channel,"status_url":f"/v1/image-generations/jobs/{jid}"}


@app.get("/v1/image-generations/jobs/{job_id}",tags=["图像生成"],summary="查询原子图像生成任务",dependencies=[Depends(require_service_token)])
async def get_image_generation_job(job_id:UUID)->dict[str,Any]:
    try:return await asyncio.to_thread(get_generation_jobs().status,str(job_id))
    except CapabilityNotFound as exc:raise HTTPException(404,"image generation job not found") from exc


@app.post("/v1/image-generations/jobs/{job_id}/cancel",tags=["图像生成"],summary="取消原子图像生成任务",dependencies=[Depends(require_service_token)])
async def cancel_image_generation_job(job_id:UUID)->dict[str,Any]:
    try:return await asyncio.to_thread(get_generation_jobs().cancel,str(job_id))
    except CapabilityNotFound as exc:raise HTTPException(404,"image generation job not found") from exc


@app.get("/internal/admin/storage/status",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_storage_status()->dict[str,Any]:
    try:return OssStorage.from_env().status()
    except RuntimeError as exc:return {"configured":False,"error":str(exc)}


MAX_UPLOAD_BYTES = 512 * 1024 * 1024


async def _store_uploaded_file(file: UploadFile, prefix: str | None) -> dict[str, Any]:
    """把上传流落盘到临时文件再转投 OSS，返回含公网直链的结果。"""
    try:
        storage = OssStorage.from_env()
        storage.normalize_prefix(prefix)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    upload_dir = get_settings().control_runtime_dir / "oss-uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temporary = upload_dir / f"{uuid4().hex}.upload"
    size = 0
    try:
        with temporary.open("wb") as output:
            while chunk := await file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "文件不能超过512MiB")
                output.write(chunk)
        if size == 0:
            raise HTTPException(422, "文件不能为空")
        return await asyncio.to_thread(
            storage.upload, temporary, file.filename or "upload.bin", file.content_type, prefix
        )
    finally:
        temporary.unlink(missing_ok=True)


@app.post(
    "/v1/uploads",
    tags=["文件上传"],
    summary="上传本地素材并取得公网直链",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_service_token)],
)
async def create_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """把本地文件存进中台自有 OSS，返回可直接作为其它能力输入的公网 HTTPS URL。

    调用方不能自选对象前缀：这是公网端点，开放前缀等于允许写入桶里任意目录
    （包括供应商结果所在的 ai-video-kernel/）。落点由 OSS_ADMIN_UPLOAD_PREFIX 决定。
    """
    return await _store_uploaded_file(file, None)


@app.post("/internal/admin/storage/upload",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_storage_upload(file:UploadFile=File(...),prefix:str|None=Form(default=None,max_length=128))->dict[str,Any]:
    return await _store_uploaded_file(file,prefix)


@app.get("/internal/admin/ai-capabilities/channels",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_channels()->dict[str,Any]: return {"items":await asyncio.to_thread(get_capability_store().channels)}


@app.post("/internal/admin/ai-capabilities/channels",include_in_schema=False,status_code=201,dependencies=[Depends(require_service_token)])
async def admin_create_channel(request:ChannelCreateRequest)->dict[str,Any]:
    try:return await asyncio.to_thread(get_capability_store().save_channel,request.model_dump())
    except sqlite3.IntegrityError as exc:raise HTTPException(409,"channel code already exists") from exc


@app.patch("/internal/admin/ai-capabilities/channels/{channel_id}",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_update_channel(channel_id:UUID,request:ChannelUpdateRequest)->dict[str,Any]:
    try:return await asyncio.to_thread(get_capability_store().save_channel,request.model_dump(exclude_unset=True),str(channel_id))
    except CapabilityNotFound as exc:raise HTTPException(404,"channel not found") from exc
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.delete("/internal/admin/ai-capabilities/channels/{channel_id}",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_delete_channel(channel_id:UUID)->dict[str,Any]:
    try:await asyncio.to_thread(get_capability_store().delete_channel,str(channel_id));return {"deleted":True}
    except CapabilityNotFound as exc:raise HTTPException(404,"channel not found") from exc
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post("/internal/admin/ai-capabilities/channels/{channel_id}/test",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_test_channel(channel_id:UUID)->dict[str,Any]:
    try:channel=await asyncio.to_thread(get_capability_store().channel,str(channel_id),private=True)
    except CapabilityNotFound as exc:raise HTTPException(404,"channel not found") from exc
    ok,balance,error=await asyncio.to_thread(probe_channel,channel)
    saved=await asyncio.to_thread(get_capability_store().record_probe,str(channel_id),ok,error,balance)
    return {"ok":ok,"channel":saved,"error":error}


@app.get("/internal/admin/ai-capabilities/models",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_models()->dict[str,Any]:return {"items":await asyncio.to_thread(get_capability_store().models)}


@app.get("/internal/admin/ai-capabilities/jobs",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_generation_jobs(limit:int=Query(default=100,ge=1,le=500))->dict[str,Any]:return {"items":await asyncio.to_thread(get_capability_store().list_jobs,limit)}


@app.get("/internal/admin/ai-capabilities/jobs/{job_id}",include_in_schema=False,dependencies=[Depends(require_service_token)])
async def admin_generation_job(job_id:UUID)->dict[str,Any]:
    try:return await asyncio.to_thread(get_capability_store().job,str(job_id),include_request=True)
    except CapabilityNotFound as exc:raise HTTPException(404,"generation job not found") from exc


async def _validate_h3_urls(request: H3VideoJobRequest) -> None:
    try:
        for value in (*request.reference_video_urls, *request.reference_image_urls, *request.reference_audio_urls):
            if value:
                await asyncio.to_thread(validate_public_https_url, value)
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post(
    "/v1/video-generations/minimax-h3/jobs",
    tags=["MiniMax H3视频生成"],
    summary="创建异步MiniMax H3视频生成任务",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_h3_job(request: H3VideoJobRequest) -> dict[str, Any]:
    await _validate_h3_urls(request)
    task_payload = request.model_dump()
    task_payload["segment_mode"] = "single"
    task_payload["prompts"] = [task_payload.pop("prompt")]
    task_payload["split_seconds"] = None
    try:
        job = await asyncio.to_thread(get_h3_jobs().submit, task_payload)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"unable to enqueue MiniMax H3 job: {type(exc).__name__}",
        ) from exc
    model_width, model_height, output_width, output_height = resolve_dimensions(
        request.resolution, request.aspect_ratio, request.width, request.height, request.quality
    )
    return {
        "job_id": job.id,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "priority": request.priority,
        "resolution": request.resolution,
        "quality": request.quality,
        "aspect_ratio": request.aspect_ratio,
        "output_width": output_width,
        "output_height": output_height,
        "model_width": model_width,
        "model_height": model_height,
        "status_url": f"/v1/video-generations/minimax-h3/jobs/{job.id}",
        "queue_position_url": f"/v1/video-generations/minimax-h3/jobs/{job.id}/queue-position",
    }


@app.get(
    "/v1/video-generations/minimax-h3/jobs/{job_id}",
    tags=["MiniMax H3视频生成"],
    summary="查询MiniMax H3视频生成任务",
    dependencies=[Depends(require_service_token)],
)
async def get_h3_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_h3_jobs().status, str(job_id))
    except H3JobNotFound as exc:
        raise HTTPException(status_code=404, detail="MiniMax H3 job not found") from exc


@app.get(
    "/v1/video-generations/minimax-h3/jobs/{job_id}/queue-position",
    tags=["MiniMax H3视频生成"],
    summary="查询MiniMax H3任务排队位置",
    dependencies=[Depends(require_service_token)],
)
async def get_h3_queue_position(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_h3_jobs().queue_position, str(job_id))
    except H3JobNotFound as exc:
        raise HTTPException(status_code=404, detail="MiniMax H3 job not found") from exc


@app.get(
    "/v1/video-generations/minimax-h3/workers/status",
    tags=["MiniMax H3视频生成"],
    summary="查询MiniMax H3 Worker汇总状态",
    dependencies=[Depends(require_service_token)],
)
async def get_h3_worker_status() -> dict[str, Any]:
    return await asyncio.to_thread(get_h3_jobs().worker_summary)


@app.post(
    "/v1/video-generations/minimax-h3/jobs/{job_id}/cancel",
    tags=["MiniMax H3视频生成"],
    summary="取消MiniMax H3视频生成任务",
    dependencies=[Depends(require_service_token)],
)
async def cancel_h3_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_h3_jobs().cancel, str(job_id))
    except H3JobNotFound as exc:
        raise HTTPException(status_code=404, detail="MiniMax H3 job not found") from exc


@app.get(
    "/internal/admin/h3/jobs",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_list_h3_jobs(limit: int = 100, job_status: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(get_h3_store().list_jobs, min(max(limit, 1), 500), job_status)


@app.get(
    "/internal/admin/h3/jobs/{job_id}",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_get_h3_job(job_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_h3_store().job, str(job_id), include_request=True)
    except H3JobNotFound as exc:
        raise HTTPException(status_code=404, detail="MiniMax H3 job not found") from exc


@app.get(
    "/internal/admin/h3/workers",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_list_h3_workers() -> dict[str, Any]:
    store = get_h3_store()
    workers, summary = await asyncio.gather(
        asyncio.to_thread(store.list_workers),
        asyncio.to_thread(store.worker_summary),
    )
    return {"workers": workers, "summary": summary}


@app.get(
    "/internal/admin/h3/pool",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_h3_pool_status() -> dict[str, Any]:
    return await asyncio.to_thread(get_h3_pool().status)


@app.get(
    "/internal/admin/h3/pool/deployments",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_h3_pool_deployments(
    availability: Literal["available", "unavailable"] = "unavailable",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    deployment_status: str | None = Query(default=None, alias="status", max_length=32),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        get_h3_pool().page_deployments,
        availability=availability,
        page=page,
        page_size=page_size,
        status=deployment_status,
    )


@app.put(
    "/internal/admin/h3/pool/config",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_update_h3_pool_config(request: H3PoolConfigRequest) -> dict[str, Any]:
    return await asyncio.to_thread(get_h3_store().update_pool_config, request.model_dump())


@app.post(
    "/internal/admin/h3/pool/workers",
    include_in_schema=False,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def admin_create_managed_h3_worker(request: H3ManagedWorkerCreateRequest) -> dict[str, Any]:
    try:
        item = await asyncio.to_thread(
            get_h3_pool().request_worker,
            request.machine_type,
            provider_mode=request.provider_mode,
        )
        return {"accepted": True, "deployment": item}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete(
    "/internal/admin/h3/pool/workers/{deployment_id}",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_delete_managed_h3_worker(deployment_id: UUID) -> dict[str, Any]:
    try:
        await asyncio.to_thread(get_h3_pool().retire, str(deployment_id))
        return {"deleted": True, "deployment_id": str(deployment_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="managed H3 worker not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/internal/admin/h3/workers",
    include_in_schema=False,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_service_token)],
)
async def admin_create_h3_worker(request: H3WorkerCreateRequest) -> dict[str, Any]:
    try:
        await asyncio.to_thread(validate_public_https_url, request.base_url)
        return await asyncio.to_thread(
            get_h3_store().create_worker, request.name, request.base_url, request.enabled
        )
    except MediaFetchError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail="worker name or URL is already registered") from exc


@app.patch(
    "/internal/admin/h3/workers/{worker_id}",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_update_h3_worker(worker_id: UUID, request: H3WorkerUpdateRequest) -> dict[str, Any]:
    values = request.model_dump(exclude_unset=True)
    if values.get("base_url"):
        try:
            await asyncio.to_thread(validate_public_https_url, str(values["base_url"]))
        except MediaFetchError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
    try:
        return await asyncio.to_thread(get_h3_store().update_worker, str(worker_id), values)
    except H3WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail="H3 worker not found") from exc


@app.delete(
    "/internal/admin/h3/workers/{worker_id}",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_delete_h3_worker(worker_id: UUID) -> dict[str, Any]:
    try:
        await asyncio.to_thread(get_h3_store().delete_worker, str(worker_id))
    except H3WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail="H3 worker not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True, "worker_id": str(worker_id)}


@app.post(
    "/internal/admin/h3/workers/{worker_id}/test",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_test_h3_worker(worker_id: UUID) -> dict[str, Any]:
    try:
        worker = await asyncio.to_thread(get_h3_store().worker, str(worker_id))
        internal = await asyncio.to_thread(get_h3_store().worker, str(worker_id), public=False)
    except H3WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail="H3 worker not found") from exc
    result = await asyncio.to_thread(
        probe_worker,
        str(internal["base_url"]),
        get_settings().h3_health_timeout_seconds,
        check_compatibility=True,
    )
    updated = await asyncio.to_thread(get_h3_store().record_probe, str(worker_id), **result)
    return {"worker": updated, "connection": {"ok": result["ok"], "compatible": result["compatible"], "error": result.get("error")}}


@app.post(
    "/internal/admin/h3/workers/{worker_id}/drain",
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)
async def admin_drain_h3_worker(worker_id: UUID) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_h3_store().update_worker, str(worker_id), {"draining": True})
    except H3WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail="H3 worker not found") from exc


@app.get("/internal/admin/api-keys", include_in_schema=False, dependencies=[Depends(require_service_token)])
async def admin_list_api_keys() -> dict[str, Any]:
    return {"items": await asyncio.to_thread(get_api_key_store().list)}


@app.post("/internal/admin/api-keys", include_in_schema=False, status_code=201, dependencies=[Depends(require_service_token)])
async def admin_create_api_key(request: ApiKeyCreateRequest) -> dict[str, Any]:
    expiration = request.expires_at.astimezone(timezone.utc).isoformat(timespec="milliseconds") if request.expires_at else None
    return await asyncio.to_thread(get_api_key_store().create, request.name, request.quota, expiration)


@app.patch("/internal/admin/api-keys/{key_id}", include_in_schema=False, dependencies=[Depends(require_service_token)])
async def admin_update_api_key(key_id: UUID, request: ApiKeyUpdateRequest) -> dict[str, Any]:
    changes = request.model_dump(exclude_unset=True)
    if isinstance(changes.get("expires_at"), datetime):
        changes["expires_at"] = changes["expires_at"].astimezone(timezone.utc).isoformat(timespec="milliseconds")
    result = await asyncio.to_thread(get_api_key_store().update, str(key_id), **changes)
    if result is None:
        raise HTTPException(status_code=404, detail="api key not found")
    return result


@app.delete("/internal/admin/api-keys/{key_id}", include_in_schema=False, dependencies=[Depends(require_service_token)])
async def admin_delete_api_key(key_id: UUID) -> dict[str, Any]:
    if not await asyncio.to_thread(get_api_key_store().delete, str(key_id)):
        raise HTTPException(status_code=404, detail="api key not found")
    return {"deleted": True, "id": str(key_id)}
