from __future__ import annotations

import threading
import os
import site
import ssl
import sys
import ctypes
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .config import OcrConfig
from .schemas import ImageInput, ImageResult, OcrItem, Region


class OcrEngineUnavailable(RuntimeError):
    pass


_DLL_DIRECTORY_HANDLES: list[Any] = []


class PaddleOcrEngine:
    def __init__(self, config: OcrConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._ocr: Any | None = None
        self._load_error: str | None = None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "engine": self.config.engine,
            "model_version": self.config.model_version,
            "device": self.config.device,
            "loaded": self._ocr is not None,
            "load_error": self._load_error,
        }

    def _load(self) -> Any:
        with self._lock:
            if self._ocr is not None:
                return self._ocr
            try:
                _register_windows_nvidia_dll_dirs()
                _register_certifi_bundle()
                os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
                os.environ.setdefault("FLAGS_use_mkldnn", "0")
                os.environ.setdefault("FLAGS_use_onednn", "0")
                from paddleocr import PaddleOCR

                self._ocr = PaddleOCR(
                    text_detection_model_dir=self.config.text_detection_model_dir,
                    text_recognition_model_dir=self.config.text_recognition_model_dir,
                    use_doc_orientation_classify=self.config.use_doc_orientation_classify,
                    use_doc_unwarping=self.config.use_doc_unwarping,
                    use_textline_orientation=self.config.use_textline_orientation,
                    device=self.config.device,
                    enable_hpi=False,
                    enable_mkldnn=False,
                    enable_cinn=False,
                )
            except Exception as exc:  # pragma: no cover - depends on local model/runtime install
                self._load_error = str(exc)
                raise OcrEngineUnavailable(str(exc)) from exc
            return self._ocr

    def recognize_batch(self, images: list[ImageInput]) -> list[ImageResult]:
        ocr = self._load()
        return [self._recognize_image(ocr, image) for image in images]

    def _recognize_image(self, ocr: Any, image: ImageInput) -> ImageResult:
        pil_image = Image.open(image.path).convert("RGB")
        regions = image.regions or [Region(name="full", bbox=None)]
        items: list[OcrItem] = []
        for region in regions:
            crop, offset_x, offset_y = self._crop_region(pil_image, region)
            raw = self._predict(ocr, crop)
            items.extend(self._normalize_items(raw, region.name, offset_x, offset_y))
        return ImageResult(image_id=image.image_id, time=image.time, items=items)

    @staticmethod
    def _crop_region(image: Image.Image, region: Region) -> tuple[np.ndarray, int, int]:
        if region.bbox is None:
            return np.asarray(image), 0, 0
        x1, y1, x2, y2 = region.bbox
        width, height = image.size
        x1 = max(0, min(width, int(x1)))
        x2 = max(0, min(width, int(x2)))
        y1 = max(0, min(height, int(y1)))
        y2 = max(0, min(height, int(y2)))
        if x2 <= x1 or y2 <= y1:
            return np.asarray(image.crop((0, 0, 1, 1))), 0, 0
        return np.asarray(image.crop((x1, y1, x2, y2))), x1, y1

    @staticmethod
    def _predict(ocr: Any, image_array: np.ndarray) -> Any:
        if hasattr(ocr, "predict"):
            return ocr.predict(image_array)
        return ocr.ocr(image_array, cls=False)

    @staticmethod
    def _normalize_items(raw: Any, region_name: str, offset_x: int, offset_y: int) -> list[OcrItem]:
        items: list[OcrItem] = []
        for entry in _iter_ocr_entries(raw):
            bbox, text, score = entry
            xs = [int(point[0]) + offset_x for point in bbox]
            ys = [int(point[1]) + offset_y for point in bbox]
            if not text:
                continue
            items.append(
                OcrItem(
                    bbox=[min(xs), min(ys), max(xs), max(ys)],
                    text=text,
                    score=float(score),
                    region=region_name,
                )
            )
        return items


def _iter_ocr_entries(raw: Any) -> list[tuple[list[list[float]], str, float]]:
    entries: list[tuple[list[list[float]], str, float]] = []
    if raw is None:
        return entries
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entries.extend(_entries_from_dict(item))
            elif isinstance(item, list):
                entries.extend(_entries_from_legacy_list(item))
    elif isinstance(raw, dict):
        entries.extend(_entries_from_dict(raw))
    return entries


def _entries_from_dict(item: dict[str, Any]) -> list[tuple[list[list[float]], str, float]]:
    boxes = item.get("dt_polys") or item.get("rec_polys") or item.get("boxes") or []
    texts = item.get("rec_texts") or item.get("texts") or []
    scores = item.get("rec_scores") or item.get("scores") or []
    entries: list[tuple[list[list[float]], str, float]] = []
    for box, text, score in zip(boxes, texts, scores):
        entries.append((_box_to_points(box), str(text), float(score)))
    return entries


def _entries_from_legacy_list(items: list[Any]) -> list[tuple[list[list[float]], str, float]]:
    entries: list[tuple[list[list[float]], str, float]] = []
    for line in items:
        if not isinstance(line, list) or len(line) < 2:
            continue
        box = _box_to_points(line[0])
        text_score = line[1]
        if isinstance(text_score, (list, tuple)) and len(text_score) >= 2:
            entries.append((box, str(text_score[0]), float(text_score[1])))
    return entries


def _box_to_points(box: Any) -> list[list[float]]:
    array = np.asarray(box, dtype=float).reshape(-1, 2)
    return [[float(x), float(y)] for x, y in array.tolist()]


def assert_image_paths_exist(images: list[ImageInput]) -> None:
    for image in images:
        if not Path(image.path).is_file():
            raise FileNotFoundError(image.path)


def _register_windows_nvidia_dll_dirs() -> None:
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    library_bin = Path(sys.prefix) / "Library" / "bin"
    if library_bin.is_dir():
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(library_bin)))
        os.environ["PATH"] = f"{library_bin}{os.pathsep}" + os.environ.get("PATH", "")
    torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
    if torch_lib.is_dir():
        os.environ["PATH"] = os.pathsep.join(
            path
            for path in os.environ.get("PATH", "").split(os.pathsep)
            if Path(path) != torch_lib
        )
    roots = [Path(sys.prefix) / "Lib" / "site-packages"]
    roots.extend(Path(path) for path in site.getsitepackages())
    seen: set[Path] = set()
    for root in roots:
        candidate = root / "nvidia"
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_dir():
            for bin_dir in candidate.glob("*/bin"):
                if bin_dir.is_dir():
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(bin_dir)))
                    os.environ["PATH"] = f"{bin_dir}{os.pathsep}" + os.environ.get("PATH", "")
            for dll_name in [
                "zlibwapi.dll",
                "cudart64_12.dll",
                "cublas64_12.dll",
                "cublasLt64_12.dll",
                "cudnn64_9.dll",
                "cudnn_ops64_9.dll",
                "cudnn_cnn64_9.dll",
            ]:
                try:
                    ctypes.CDLL(dll_name)
                except OSError:
                    pass
            return


def _register_certifi_bundle() -> None:
    try:
        import certifi
    except Exception:
        return
    bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
    if os.name == "nt" and not getattr(ssl, "_ocr_certifi_patched", False):
        original_create_default_context = ssl.create_default_context

        def create_default_context_with_certifi(*args: Any, **kwargs: Any) -> ssl.SSLContext:
            kwargs.setdefault("cafile", bundle)
            return original_create_default_context(*args, **kwargs)

        ssl.create_default_context = create_default_context_with_certifi
        ssl._ocr_certifi_patched = True
