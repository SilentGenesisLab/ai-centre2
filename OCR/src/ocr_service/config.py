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
    text_detection_model_dir: str | None = None
    text_recognition_model_dir: str | None = None
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_textline_orientation: bool = False
    max_images_per_request: int = 128
    max_regions_per_image: int = 8


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
