from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_token: str
    control_host: str = "127.0.0.1"
    control_port: int = 8320
    control_runtime_dir: Path = Path("/home/donxu/ai-centre/runtime/control")
    asr_backend_url: str = "http://127.0.0.1:9001"
    tts_backend_url: str = "http://127.0.0.1:8191"
    upstream_timeout_seconds: float = 900
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
    return settings

