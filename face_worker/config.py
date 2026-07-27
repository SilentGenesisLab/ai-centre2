from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_token: str
    redis_broker_url: str = "redis://127.0.0.1:6379/12"
    redis_result_url: str = "redis://127.0.0.1:6379/13"
    work_dir: Path = Path("/home/donxu/ai-centre/data")
    face_model_path: Path = Path("/home/donxu/ai-centre/models/yolov8n-face.onnx")
    face_conf_threshold: float = 0.25
    face_iou_threshold: float = 0.45
    face_mosaic_size: int = 16
    face_frame_skip: int = 0
    kernel_upload_url: str
    kernel_api_token: str
    download_timeout_sec: float = 300
    upload_timeout_sec: float = 300
    callback_timeout_sec: float = 20

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings

