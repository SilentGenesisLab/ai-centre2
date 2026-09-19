from __future__ import annotations

import json
from pathlib import Path

from control_plane.video_review_pipeline import _detect_shots, _extract_json, _render_markdown


def test_detect_shots_returns_ordered_non_overlapping_ranges() -> None:
    source = Path(r"I:\self_tool\ai-centre2\data\videos\英文\(no caption)_7664637799731039509.mp4")
    if not source.is_file():
        return
    shots = _detect_shots(source)
    assert shots
    assert shots[0]["start_sec"] == 0.0
    for previous, current in zip(shots, shots[1:]):
        assert previous["end_sec"] <= current["start_sec"] + 1e-6
        assert current["end_sec"] > current["start_sec"]


def test_markdown_report_keeps_evidence_timecodes() -> None:
    report = {
        "job_id": "review-test",
        "source": {"duration_sec": 2.0},
        "shots": [{"shot_id": "S001", "start_sec": 0.0, "end_sec": 2.0, "subject": "人物", "action": "回头", "evidence_confidence": "high"}],
        "issues": [{"id": "Q-001", "severity": "major", "time_range": {"start_sec": 0.4, "end_sec": 0.8}, "observation": "动作跳变", "recommendation": "局部复核"}],
        "assumptions_and_limits": [],
    }
    markdown = _render_markdown(report)
    assert "S001" in markdown
    assert "0.400-0.800s" in markdown


def test_json_extractor_accepts_trailing_model_explanation() -> None:
    assert _extract_json('[{"shot_id":"S001"}]\n这里是补充说明') == [{"shot_id": "S001"}]
