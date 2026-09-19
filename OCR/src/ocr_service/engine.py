from __future__ import annotations

import gc
import threading
import os
import site
import ssl
import sys
import ctypes
from collections.abc import Iterable
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
        self._thai_ocr: Any | None = None
        self._load_error: str | None = None
        self._completed_batches = 0
        self._recycle_reason: str | None = None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "engine": self.config.engine,
            "model_version": self.config.model_version,
            "device": self.config.device,
            "loaded": self._ocr is not None,
            "thai_loaded": self._thai_ocr is not None,
            "load_error": self._load_error,
            "completed_batches": self._completed_batches,
            "rss_mb": round(_process_rss_mb(), 1),
            "recycle_reason": self._recycle_reason,
        }

    def _load(self, route: str = "default") -> Any:
        with self._lock:
            current = self._thai_ocr if route == "thai" else self._ocr
            if current is not None:
                return current
            try:
                _register_windows_nvidia_dll_dirs()
                _register_certifi_bundle()
                os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
                os.environ.setdefault("FLAGS_use_mkldnn", "0")
                os.environ.setdefault("FLAGS_use_onednn", "0")
                from paddleocr import PaddleOCR

                recognition_dir = (
                    self.config.thai_text_recognition_model_dir
                    if route == "thai"
                    else self.config.text_recognition_model_dir
                )
                recognition_name = (
                    self.config.thai_text_recognition_model_name
                    if route == "thai"
                    else self.config.text_recognition_model_name
                )
                if route == "thai" and not recognition_dir:
                    raise OcrEngineUnavailable("Thai recognition model is not configured")
                current = PaddleOCR(
                    text_detection_model_name=self.config.text_detection_model_name,
                    text_detection_model_dir=self.config.text_detection_model_dir,
                    text_recognition_model_name=recognition_name,
                    text_recognition_model_dir=recognition_dir,
                    use_doc_orientation_classify=self.config.use_doc_orientation_classify,
                    use_doc_unwarping=self.config.use_doc_unwarping,
                    use_textline_orientation=self.config.use_textline_orientation,
                    device=self.config.device,
                    enable_hpi=False,
                    enable_mkldnn=False,
                    enable_cinn=False,
                )
                if route == "thai":
                    self._thai_ocr = current
                else:
                    self._ocr = current
            except Exception as exc:  # pragma: no cover - depends on local model/runtime install
                self._load_error = str(exc)
                raise OcrEngineUnavailable(str(exc)) from exc
            return current

    def recognize_batch(
        self, images: list[ImageInput], source_lang_hint: str | None = None
    ) -> list[ImageResult]:
        ocr = self._load(self._route(source_lang_hint))
        try:
            return [self._recognize_image(ocr, image) for image in images]
        finally:
            self._complete_batch()

    @property
    def recycle_reason(self) -> str | None:
        return self._recycle_reason

    def _complete_batch(self) -> None:
        self._completed_batches += 1
        trim_interval = self.config.memory_trim_interval_batches
        if trim_interval > 0 and self._completed_batches % trim_interval == 0:
            _trim_process_memory()
        if self._recycle_reason is not None:
            return
        max_batches = self.config.worker_max_batches
        if max_batches > 0 and self._completed_batches >= max_batches:
            self._recycle_reason = f"completed {self._completed_batches} OCR batches"
            return
        max_rss_mb = self.config.worker_max_rss_mb
        rss_mb = _process_rss_mb()
        if max_rss_mb > 0 and rss_mb >= max_rss_mb:
            self._recycle_reason = f"RSS {rss_mb:.0f} MB reached limit {max_rss_mb} MB"

    def _recognize_image(self, ocr: Any, image: ImageInput) -> ImageResult:
        with Image.open(image.path) as source_image:
            pil_image = source_image.convert("RGB")
        try:
            regions = image.regions or [Region(name="full", bbox=None)]
            items: list[OcrItem] = []
            for region in regions:
                crop, offset_x, offset_y = self._crop_region(pil_image, region)
                raw = None
                try:
                    raw = self._predict(ocr, crop)
                    items.extend(self._normalize_items(raw, region.name, offset_x, offset_y))
                finally:
                    close = getattr(raw, "close", None)
                    if callable(close):
                        close()
                    del raw, crop
            return ImageResult(image_id=image.image_id, time=image.time, items=items)
        finally:
            pil_image.close()

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
    def _route(source_lang_hint: str | None) -> str:
        language = (source_lang_hint or "").lower()
        return "thai" if language == "th" or language.startswith("th-") else "default"

    def recognition_model_name(self, source_lang_hint: str | None) -> str:
        if self._route(source_lang_hint) == "thai":
            return self.config.thai_text_recognition_model_name or "th_PP-OCRv5_mobile_rec"
        return self.config.text_recognition_model_name or self.config.model_version

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
    if not isinstance(raw, (dict, list, str, bytes)) and isinstance(raw, Iterable):
        raw = list(raw)
    if isinstance(raw, list):
        for item in raw:
            payload = getattr(item, "json", item)
            if callable(payload):
                payload = payload()
            item = payload
            if isinstance(item, dict):
                entries.extend(_entries_from_dict(item))
            elif isinstance(item, list):
                entries.extend(_entries_from_legacy_list(item))
    elif isinstance(raw, dict):
        entries.extend(_entries_from_dict(raw))
    return entries


def _entries_from_dict(item: dict[str, Any]) -> list[tuple[list[list[float]], str, float]]:
    if isinstance(item.get("res"), dict):
        item = item["res"]
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


def _process_rss_mb() -> float:
    try:
        statm = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(statm[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return 0.0


def _trim_process_memory() -> None:
    gc.collect()
    if sys.platform != "linux":
        return
    try:
        malloc_trim = ctypes.CDLL(None).malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        malloc_trim(0)
    except (AttributeError, OSError):
        pass


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
