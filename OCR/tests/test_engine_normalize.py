from __future__ import annotations

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
