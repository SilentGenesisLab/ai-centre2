from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OcrConfig:
    host: str = "127.0.0.1"
    port: int = 8096
    engine: str = "paddleocr"
    model_version: str = "ppocrv6"
    device: str = "gpu:0"
    text_detection_model_name: str | None = None
    text_detection_model_dir: str | None = None
    text_recognition_model_name: str | None = None
    text_recognition_model_dir: str | None = None
    thai_text_recognition_model_name: str | None = None
    thai_text_recognition_model_dir: str | None = None
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_textline_orientation: bool = False
    max_images_per_request: int = 128
    max_regions_per_image: int = 8
    memory_trim_interval_batches: int = 100
    worker_max_rss_mb: int = 0
    worker_max_batches: int = 0
    preload_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerEndpoint:
    name: str
    url: str


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8096
    request_timeout_seconds: float = 120.0
    workers: tuple[WorkerEndpoint, ...] = ()


def load_config(path: str | os.PathLike[str] | None = None) -> OcrConfig:
    config_path = Path(
        path
        or os.environ.get("OCR_CONFIG")
        or Path(__file__).resolve().parents[2] / "config" / "ocr.local.json"
    )
    if not config_path.is_file():
        return OcrConfig()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return OcrConfig(**{key: value for key, value in data.items() if key in OcrConfig.__annotations__})


def load_gateway_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    config_path = Path(
        path
        or os.environ.get("OCR_GATEWAY_CONFIG")
        or Path(__file__).resolve().parents[2] / "config" / "linux" / "gateway.json"
    )
    if not config_path.is_file():
        return GatewayConfig()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    workers = tuple(
        WorkerEndpoint(name=str(worker["name"]), url=str(worker["url"]).rstrip("/"))
        for worker in data.get("workers", [])
    )
    return GatewayConfig(
        host=str(data.get("host", "127.0.0.1")),
        port=int(data.get("port", 8096)),
        request_timeout_seconds=float(data.get("request_timeout_seconds", 120.0)),
        workers=workers,
    )
