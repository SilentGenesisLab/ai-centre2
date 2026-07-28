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
    asr_backend_url: str = "http://127.0.0.1:9001"
    tts_backend_url: str = "http://127.0.0.1:8191"
    upstream_timeout_seconds: float = 900
    redis_broker_url: str = "redis://127.0.0.1:6379/12"
    redis_result_url: str = "redis://127.0.0.1:6379/13"
    tts_voice_registry_path: Path = Path(
        "/home/donxu/ai-centre/runtime/control/tts-voices.json"
    )
    tts_output_dir: Path = Path("/home/donxu/ai-centre/runtime/control/tts-output")
    tts_ffmpeg_bin: str = "auto"
    tts_job_workers: int = 8
    tts_result_expires_seconds: int = 604800
    tts_auto_provider_order: str = "voxcpm2,doubao,elevenlabs"

    voxcpm2_tts_enabled: bool = True
    voxcpm2_tts_max_concurrency: int = 4

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
    settings.tts_voice_registry_path.parent.mkdir(parents=True, exist_ok=True)
    settings.tts_output_dir.mkdir(parents=True, exist_ok=True)
    return settings
