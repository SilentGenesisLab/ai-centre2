from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_token: str
    control_host: str = "127.0.0.1"
    control_port: int = 8320
    control_runtime_dir: Path = Path("/home/donxu/ai-centre/runtime/control")
    api_keys_db_path: Path = Path("/home/donxu/ai-centre/runtime/control/api-keys.db")
    runtime_settings_db_path: Path = Path("/home/donxu/ai-centre/runtime/control/runtime-settings.db")
    ai_capabilities_db_path: Path = Path("/home/donxu/ai-centre/runtime/ai-capabilities/capabilities.db")
    video_generation_blank_image_url: str = (
        "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/"
        "temp_2/%E7%A9%BA%E7%99%BD.png"
    )
    video_generation_work_dir: Path = Path(
        "/home/donxu/ai-centre/runtime/video-generation"
    )
    video_generation_result_max_bytes: int = 2 * 1024 * 1024 * 1024
    video_generation_download_timeout_seconds: float = 900
    video_generation_upload_timeout_seconds: float = 900
    audio_generation_work_dir: Path = Path(
        "/home/donxu/ai-centre/runtime/audio-generation"
    )
    audio_generation_result_max_bytes: int = 512 * 1024 * 1024
    audio_generation_download_timeout_seconds: float = 600
    audio_generation_transcode_timeout_seconds: float = 900
    audio_generation_upload_timeout_seconds: float = 600
    observability_db_path: Path = Path(
        "/home/donxu/ai-centre/runtime/observability/observability.db"
    )
    observability_payload_key: SecretStr | None = None
    observability_payload_retention_days: int = 30
    observability_record_retention_days: int = 365
    observability_reconcile_seconds: float = 5.0
    health_monitor_db_path: Path = Path(
        "/home/donxu/ai-centre/runtime/health-monitor/health-monitor.db"
    )
    health_monitor_key: SecretStr | None = None
    health_monitor_l1_seconds: int = 1800
    health_monitor_l2_seconds: int = 21600
    health_monitor_paid_seconds: int = 86400
    health_monitor_jitter_seconds: int = 300
    health_monitor_failure_threshold: int = 2
    health_monitor_recovery_threshold: int = 2
    health_monitor_probe_asset_base_url: str | None = None
    health_monitor_probe_audio_url: str | None = "https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/e4568442aa0a4443a0188580c7d3a520.wav"
    health_monitor_probe_image_url: str | None = "https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/98185200d0bc4087aba50b1e30f5f0d3.png"
    health_monitor_probe_video_url: str | None = "https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/d545a39f58ad4462991180b9d38765f8.mp4"
    health_monitor_probe_face_video_url: str | None = "https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/dc8c09c0bc244f1f8f38dcba8cb97fb8.mp4"
    health_monitor_public_admin_url: str = "https://aicentre2.sligenai.cn:8443/admin/resources"
    asr_backend_url: str = "http://127.0.0.1:9001"
    tts_backend_url: str = "http://127.0.0.1:8193"
    speaker_verify_url: str = "http://127.0.0.1:8195"
    emotion_verify_url: str = "http://127.0.0.1:8196"
    tts_speaker_similarity_threshold: float = 0.31
    tts_emotion_score_threshold: float = 0.45
    tts_quality_expires_seconds: int = 604800
    tts_enhanced_pipeline_enabled: bool = True
    musetalk_backend_url: str = "http://127.0.0.1:9011"
    ocr_gateway_url: str = "http://127.0.0.1:8096"
    subtitle_api_url: str = "http://127.0.0.1:8098"
    face_api_url: str = "http://127.0.0.1:8310"
    face_wait_timeout_seconds: float = 1800
    scene_wait_timeout_seconds: float = 1800
    scene_work_dir: Path = Path("/home/donxu/ai-centre/runtime/scene-detect")
    scene_max_download_bytes: int = 512 * 1024 * 1024
    scene_download_timeout_seconds: float = 900
    scene_ffmpeg_timeout_seconds: float = 3600
    scene_upload_timeout_seconds: float = 300
    scene_result_expires_seconds: int = 604800
    video_review_work_dir: Path = Path("/home/donxu/ai-centre/runtime/video-reviews")
    video_review_max_download_bytes: int = 512 * 1024 * 1024
    video_review_max_duration_seconds: int = 600
    video_review_download_timeout_seconds: float = 900
    video_review_result_expires_seconds: int = 604800
    video_review_ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    video_review_ark_api_key: SecretStr | None = None
    video_review_ark_model: str | None = None
    video_review_ark_timeout_seconds: float = 120
    video_review_store_remote: bool = False
    watermark_wait_timeout_seconds: float = 3600
    watermark_work_dir: Path = Path("/home/donxu/ai-centre/runtime/watermark-removal")
    watermark_max_download_bytes: int = 512 * 1024 * 1024
    watermark_download_timeout_seconds: float = 900
    watermark_ffmpeg_timeout_seconds: float = 3600
    watermark_upload_timeout_seconds: float = 300
    watermark_result_expires_seconds: int = 604800
    watermark_intermediate_retention_seconds: int = 86400
    watermark_ffmpeg_bin: str = "auto"
    depth_wait_timeout_seconds: float = 3600
    depth_work_dir: Path = Path("/home/donxu/ai-centre/runtime/video-depth")
    depth_source_dir: Path = Path("/home/donxu/services/video-depth-anything-small")
    depth_checkpoint_path: Path = Path(
        "/home/donxu/services/video-depth-anything-small/checkpoints/video_depth_anything_vits.pth"
    )
    depth_da2_base_checkpoint_path: Path = Path(
        "/home/donxu/services/depth-models/da2/base/video_depth_anything_vitb.pth"
    )
    depth_da2_base_max_input_size: int = 392
    depth_da3_source_dir: Path = Path(
        "/home/donxu/services/depth-models/da3/source"
    )
    depth_da3_small_model_dir: Path = Path(
        "/home/donxu/services/depth-models/da3/small"
    )
    depth_da3_base_model_dir: Path = Path(
        "/home/donxu/services/depth-models/da3/base"
    )
    depth_da3_chunk_size: int = 12
    depth_da3_chunk_overlap: int = 4
    depth_max_download_bytes: int = 512 * 1024 * 1024
    depth_download_timeout_seconds: float = 900
    depth_upload_timeout_seconds: float = 300
    depth_result_expires_seconds: int = 604800
    audio_separation_wait_timeout_seconds: float = 3600
    audio_separation_work_dir: Path = Path(
        "/home/donxu/ai-centre/runtime/audio-separation"
    )
    audio_separation_source_dir: Path = Path(
        "/home/donxu/services/audio-separation-bandit"
    )
    audio_separation_python: Path = Path(
        "/home/donxu/ai-centre/.venv-tts-v026/bin/python"
    )
    audio_separation_checkpoint_path: Path = Path(
        "/home/donxu/services/audio-separation-bandit/checkpoint-multi.slim.pt"
    )
    audio_separation_batch_size: int = 4
    audio_separation_peak_limit_dbfs: float = -1.0
    audio_separation_max_download_bytes: int = 512 * 1024 * 1024
    audio_separation_download_timeout_seconds: float = 900
    audio_separation_ffmpeg_timeout_seconds: float = 3600
    audio_separation_upload_timeout_seconds: float = 300
    audio_separation_result_expires_seconds: int = 604800
    color_grade_work_dir: Path = Path(
        "/home/donxu/ai-centre/runtime/color-grade"
    )
    color_grade_max_download_bytes: int = 2 * 1024 * 1024 * 1024
    color_grade_cube_max_bytes: int = 8 * 1024 * 1024
    color_grade_download_timeout_seconds: float = 900
    color_grade_ffmpeg_timeout_seconds: float = 7200
    color_grade_upload_timeout_seconds: float = 900
    color_grade_result_expires_seconds: int = 604800
    color_grade_ffmpeg_bin: str = "auto"
    color_grade_video_encoder: str = "libx264"
    color_grade_video_bitrate: str = "14M"
    runninghub_base_url: str = "https://www.runninghub.cn"
    runninghub_api_token: SecretStr | None = None
    video_upscale_auto_provider_order: str = "flashvsr_v2,flashvsr,seedvr2"
    video_upscale_wait_timeout_seconds: float = 14400
    video_upscale_provider_timeout_seconds: float = 7200
    video_upscale_request_timeout_seconds: float = 60
    video_upscale_poll_seconds: float = 5
    video_upscale_result_expires_seconds: int = 604800
    video_upscale_work_dir: Path = Path("/home/donxu/ai-centre/runtime/video-upscale")
    video_upscale_segment_seconds: float = 11.8
    # 每段请求多带的真实帧数：head 供上游预热（治首帧崩坏），tail 兜上游丢尾帧（治每段停滞）。
    # 两者都从源片自己的相邻段里取，合并时丢弃，不进正片。单位是帧，不是秒。
    # tail 取 20 是因为实测丢尾帧在 8~13 帧之间浮动（且可能是 8 的倍数、出现 16），
    # 余量必须比最坏情况宽，否则那一段会因为「凑不齐正片」被判失败重试。
    video_upscale_head_context_frames: int = 8
    video_upscale_tail_margin_frames: int = 20
    video_upscale_segment_concurrency: int = 3
    video_upscale_segment_attempts: int = 3
    concurrency_slot_wait_seconds: float = 7200
    video_upscale_max_download_bytes: int = 2 * 1024 * 1024 * 1024
    video_upscale_ffmpeg_timeout_seconds: float = 3600
    video_upscale_ffmpeg_bin: str = "auto"
    video_upscale_upload_timeout_seconds: float = 900
    h3_work_dir: Path = Path("/home/donxu/ai-centre/runtime/minimax-h3")
    h3_db_path: Path = Path("/home/donxu/ai-centre/runtime/minimax-h3/h3.db")
    h3_max_video_bytes: int = 512 * 1024 * 1024
    h3_max_image_bytes: int = 20 * 1024 * 1024
    h3_max_audio_bytes: int = 512 * 1024 * 1024
    h3_download_timeout_seconds: float = 900
    h3_worker_poll_seconds: float = 5
    h3_worker_timeout_seconds: float = 1800
    h3_health_interval_seconds: float = 10
    h3_health_timeout_seconds: float = 5
    h3_lease_seconds: int = 30
    h3_wait_for_worker_seconds: int = 604800
    h3_failed_retention_seconds: int = 86400
    h3_upload_timeout_seconds: float = 300
    suanli_token: str | None = None
    suanli_base_url: str = "https://openapi.suanli.cn"
    h3_pool_reconcile_seconds: float = 10
    h3_pool_startup_grace_seconds: int = 1200
    h3_worker_offline_cleanup_seconds: int = 1800
    h3_pool_balance_cooldown_seconds: int = 1800
    h3_pool_other_backoff_max_seconds: int = 600
    h3_lark_webhook_url: SecretStr | None = None
    h3_lark_webhook_secret: SecretStr | None = None
    h3_lark_timeout_seconds: float = 5
    kernel_upload_url: str | None = None
    kernel_api_token: str | None = None
    upstream_timeout_seconds: float = 900
    musetalk_timeout_seconds: float = 1800
    redis_broker_url: str = "redis://127.0.0.1:6379/12"
    redis_result_url: str = "redis://127.0.0.1:6379/13"
    tts_voice_registry_path: Path = Path(
        "/home/donxu/ai-centre/runtime/control/tts-voices.json"
    )
    tts_output_dir: Path = Path("/home/donxu/ai-centre/runtime/control/tts-output")
    tts_reference_dir: Path = Path("/home/donxu/ai-centre/runtime/references")
    remote_input_dir: Path = Path("/home/donxu/ai-centre/runtime/control/remote-inputs")
    tts_ffmpeg_bin: str = "auto"
    tts_job_workers: int = 8
    tts_upload_timeout_seconds: float = 900
    tts_result_expires_seconds: int = 604800
    tts_auto_provider_order: str = "voxcpm2,doubao,elevenlabs"

    voxcpm2_tts_enabled: bool = True
    voxcpm2_tts_max_concurrency: int = 4

    doubao_emotion_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_emotion_api_key: SecretStr | None = None
    doubao_emotion_model: str | None = None
    doubao_emotion_timeout_seconds: float = 8

    doubao_tts_enabled: bool = False
    doubao_tts_url: str = "https://openspeech.bytedance.com/api/v1/tts"
    doubao_tts_app_id: str | None = None
    doubao_tts_access_token: SecretStr | None = None
    doubao_tts_cluster: str = "volcano_tts"
    doubao_tts_max_concurrency: int = 4

    elevenlabs_tts_enabled: bool = False
    elevenlabs_tts_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_tts_api_key: SecretStr | None = None
    elevenlabs_tts_model_id: str = "eleven_multilingual_v2"
    elevenlabs_tts_output_format: str = "mp3_44100_128"
    elevenlabs_tts_max_concurrency: int = 4

    gpu0_enabled: bool = False
    gpu1_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.control_runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.api_keys_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.ai_capabilities_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.video_generation_work_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_generation_work_dir.mkdir(parents=True, exist_ok=True)
    settings.observability_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.health_monitor_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.tts_voice_registry_path.parent.mkdir(parents=True, exist_ok=True)
    settings.tts_output_dir.mkdir(parents=True, exist_ok=True)
    settings.tts_reference_dir.mkdir(parents=True, exist_ok=True)
    settings.remote_input_dir.mkdir(parents=True, exist_ok=True)
    settings.scene_work_dir.mkdir(parents=True, exist_ok=True)
    settings.video_review_work_dir.mkdir(parents=True, exist_ok=True)
    settings.watermark_work_dir.mkdir(parents=True, exist_ok=True)
    settings.depth_work_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_separation_work_dir.mkdir(parents=True, exist_ok=True)
    settings.color_grade_work_dir.mkdir(parents=True, exist_ok=True)
    settings.video_upscale_work_dir.mkdir(parents=True, exist_ok=True)
    settings.h3_work_dir.mkdir(parents=True, exist_ok=True)
    settings.h3_db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
