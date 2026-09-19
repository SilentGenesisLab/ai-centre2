from __future__ import annotations

from ocr_service.config import OcrConfig
from ocr_service.engine import PaddleOcrEngine, _iter_ocr_entries


class FakeResult:
    json = {
        "res": {
            "dt_polys": [
                [[10, 20], [110, 20], [110, 50], [10, 50]],
            ],
            "rec_texts": ["Las arrugas"],
            "rec_scores": [0.98],
        }
    }


def test_normalizes_paddlex_result_object() -> None:
    entries = _iter_ocr_entries(iter([FakeResult()]))
    assert entries == [
        (
            [[10.0, 20.0], [110.0, 20.0], [110.0, 50.0], [10.0, 50.0]],
            "Las arrugas",
            0.98,
        )
    ]


def test_routes_thai_separately() -> None:
    assert PaddleOcrEngine._route("th") == "thai"
    assert PaddleOcrEngine._route("TH") == "thai"
    assert PaddleOcrEngine._route("es") == "default"
    assert PaddleOcrEngine._route("pt-BR") == "default"


def test_reports_actual_recognition_model() -> None:
    engine = PaddleOcrEngine(
        OcrConfig(
            text_recognition_model_name="PP-OCRv6_medium_rec",
            thai_text_recognition_model_name="th_PP-OCRv5_mobile_rec",
        )
    )

    assert engine.recognition_model_name("th") == "th_PP-OCRv5_mobile_rec"
    assert engine.recognition_model_name("zh") == "PP-OCRv6_medium_rec"


def test_recognize_batch_loads_thai_model_for_thai_hint(monkeypatch) -> None:
    engine = PaddleOcrEngine(OcrConfig())
    loaded_routes = []

    def fake_load(route="default"):
        loaded_routes.append(route)
        return object()

    monkeypatch.setattr(engine, "_load", fake_load)

    assert engine.recognize_batch([], "th") == []
    assert loaded_routes == ["thai"]


def test_requests_recycle_when_rss_limit_is_reached(monkeypatch) -> None:
    engine = PaddleOcrEngine(OcrConfig(worker_max_rss_mb=1024))
    monkeypatch.setattr("ocr_service.engine._process_rss_mb", lambda: 1025.0)

    engine._complete_batch()

    assert engine.recycle_reason == "RSS 1025 MB reached limit 1024 MB"


def test_requests_recycle_after_batch_limit(monkeypatch) -> None:
    engine = PaddleOcrEngine(OcrConfig(worker_max_batches=2))
    monkeypatch.setattr("ocr_service.engine._process_rss_mb", lambda: 10.0)

    engine._complete_batch()
    engine._complete_batch()

    assert engine.recycle_reason == "completed 2 OCR batches"
