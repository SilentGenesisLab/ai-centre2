from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from control_plane.h3_metrics import infer_machine_type, mark_iqr_outliers, metric_summary
from control_plane.h3_store import H3Store


def test_iqr_marks_only_long_tail_in_same_bucket() -> None:
    rows = [
        {"resolution": "480p", "duration": 5, "mode": "text", "kind": "single", "seconds": value}
        for value in (10, 11, 12, 100)
    ]
    mark_iqr_outliers(
        rows,
        value_key="seconds",
        bucket_keys=("resolution", "duration", "mode", "kind"),
    )
    assert [row["effective_outlier"] for row in rows] == [False, False, False, True]
    assert metric_summary(row["seconds"] for row in rows if not row["effective_outlier"])["mean"] == 11.0


def test_iqr_keeps_small_buckets_untouched() -> None:
    rows = [{"bucket": "a", "seconds": value} for value in (1, 2, 999)]
    mark_iqr_outliers(rows, value_key="seconds", bucket_keys=("bucket",))
    assert not any(row["effective_outlier"] for row in rows)


def test_machine_type_is_inferred_for_manual_workers() -> None:
    assert infer_machine_type(None, "NVIDIA GeForce RTX 4090", 24 * 2**30) == "4090_24g"
    assert infer_machine_type(None, "NVIDIA GeForce RTX 4090", 48 * 2**30) == "4090_48g"
    assert infer_machine_type(None, "NVIDIA GeForce RTX 5090", 32 * 2**30) == "5090_32g"


def test_percentiles_use_nearest_rank_observations() -> None:
    summary = metric_summary(range(1, 21))
    assert summary["p50"] == 10
    assert summary["p95"] == 19


def test_h3_duration_analytics_separates_download_queue_and_generation(tmp_path) -> None:
    store = H3Store(tmp_path / "h3.db")
    worker = store.create_worker("gpu-4090", "https://worker.example")
    job_id = "11111111-1111-4111-8111-111111111111"
    store.create_job(job_id, {
        "duration_seconds": 5, "resolution": "480p", "aspect_ratio": "9:16",
        "segment_mode": "single", "prompts": ["test"],
        "reference_video_urls": [], "reference_image_urls": [], "reference_audio_urls": [],
    })
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    stages = [
        ("queued", 0), ("downloading", 10), ("waiting_for_worker", 20),
        ("uploading_to_worker", 30), ("generating_part_1", 40),
        ("rendering_delivery", 500), ("merging", 520),
        ("uploading_oss", 540), ("completed", 600),
    ]
    with store._connect() as connection:
        connection.execute(
            "UPDATE workers SET gpu_name=?,vram_total_bytes=? WHERE id=?",
            ("NVIDIA GeForce RTX 4090", 24 * 2**30, worker["id"]),
        )
        connection.execute("DELETE FROM job_stage_events WHERE job_id=?", (job_id,))
        connection.execute(
            """UPDATE jobs SET status='succeeded',attempt_count=1,created_at=?,finished_at=?,
               output_json=? WHERE id=?""",
            (start.isoformat(), (start + timedelta(seconds=600)).isoformat(),
             json.dumps({"duration_seconds": 5, "resolution": "480p"}), job_id),
        )
        connection.execute(
            """INSERT INTO attempts(job_id,attempt_number,lease_version,worker_id,status,started_at,finished_at)
               VALUES(?,?,?,?,?,?,?)""",
            (job_id, 1, "lease", worker["id"], "succeeded",
             (start + timedelta(seconds=30)).isoformat(),
             (start + timedelta(seconds=600)).isoformat()),
        )
        connection.executemany(
            "INSERT INTO job_stage_events(job_id,stage,occurred_at,source) VALUES(?,?,?,'estimated')",
            [(job_id, stage, (start + timedelta(seconds=offset)).isoformat()) for stage, offset in stages],
        )
    result = store.duration_analytics(
        (start - timedelta(seconds=1)).isoformat(),
        (start + timedelta(days=1)).isoformat(),
        machine_type="4090_24g",
    )
    five_seconds = result["durations"][1]
    assert result["succeeded_jobs"] == 1
    assert result["coverage"] == {"measured": 0, "estimated": 1}
    assert five_seconds["end_to_end"]["p95"] == 600
    assert five_seconds["stages"]["asset_download"]["mean"] == 10
    assert five_seconds["stages"]["queue_wait"]["mean"] == 10
    assert five_seconds["stages"]["generation"]["mean"] == 460
