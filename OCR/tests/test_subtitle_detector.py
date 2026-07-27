from __future__ import annotations

from ocr_service.schemas import SubtitleDetectConfig
from ocr_service.subtitle_detector import (
    _bbox_iou,
    _lines_belong_together,
    group_and_classify_frame,
    text_similarity,
)


def test_text_similarity_normalizes_punctuation() -> None:
    assert text_similarity("Las arrugas!!!", "las arrugas") > 0.95


def test_multiline_grouping_accepts_short_tail_line() -> None:
    assert _lines_belong_together([200, 100, 800, 160], [330, 170, 670, 230], 1080, 1920)


def test_centered_top_and_bottom_are_the_only_processing_groups() -> None:
    items = [
        {"bbox": [140, 120, 940, 220], "text": "Titulo principal", "score": 0.95},
        {"bbox": [260, 1500, 820, 1570], "text": "Subtitulo hablado", "score": 0.94},
        {"bbox": [20, 900, 130, 930], "text": "LOGO", "score": 0.99},
    ]
    groups = group_and_classify_frame(
        frame_index=0,
        shot_id=1,
        raw_items=items,
        width=1080,
        height=1920,
        config=SubtitleDetectConfig(),
    )
    processing = [group.type for group in groups if group.type in {"top_title", "bottom_subtitle", "cta_text"}]
    assert processing == ["top_title", "bottom_subtitle"]


def test_iou() -> None:
    assert _bbox_iou([0, 0, 100, 100], [0, 0, 100, 100]) == 1.0
    assert _bbox_iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
