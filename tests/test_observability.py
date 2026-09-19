from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.observability import CapturedCall, ObservabilityStore, external_job_id

UTC = timezone.utc


def captured(
    path: str,
    request: dict,
    response: dict | None,
    *,
    method: str = "POST",
    status_code: int = 200,
    response_type: str = "application/json",
) -> CapturedCall:
    now = datetime.now(UTC)
    return CapturedCall(
        call_id=__import__("uuid").uuid4().hex,
        trace_id=str(__import__("uuid").uuid4()),
        started_at=(now - timedelta(milliseconds=25)).isoformat(),
        method=method,
        path=path,
        query="signature=must-not-be-stored",
        client_address="127.0.0.1",
        token_fingerprint="fingerprint-only",
        request_content_type="application/json",
        request_body=json.dumps(request).encode(),
        request_bytes=len(json.dumps(request).encode()),
        status_code=status_code,
        response_content_type=response_type,
        response_headers={},
        response_body=json.dumps(response).encode() if response is not None else b"RIFFaudio",
        response_bytes=len(json.dumps(response).encode()) if response is not None else 9,
        finished_at=now.isoformat(),
        duration_ms=25,
    )


class ObservabilityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ObservabilityStore(
            Path(self.temporary.name) / "observability.db",
            "payload-secret",
            "fingerprint-secret",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_payload_is_encrypted_and_urls_are_sanitized(self) -> None:
        call = captured(
            "/v1/asr/transcriptions",
            {
                "file_url": "https://oss.example/input.wav?signature=secret&expires=123",
                "language": "auto",
                "authorization": "Bearer never-store-this",
            },
            {"text": "识别正文", "segments": [{"start": 0, "end": 2, "text": "识别正文"}]},
        )
        self.store.record(call)

        detail = self.store.call_detail(call.call_id)
        self.assertEqual(detail["request_summary"]["file_url"], "https://oss.example/input.wav?signature=%2A%2A%2A&expires=%2A%2A%2A")
        self.assertEqual(detail["request_summary"]["authorization"], "***")
        self.assertEqual(detail["request_summary"]["language"], "auto")
        self.assertEqual(detail["response_summary"]["text"], {"present": True, "characters": 4})
        revealed = self.store.reveal(call.call_id)
        self.assertEqual(revealed["response"]["payload"]["text"], "识别正文")
        database_bytes = self.store.path.read_bytes()
        self.assertNotIn("识别正文".encode(), database_bytes)
        self.assertNotIn(b"never-store-this", database_bytes)
        self.assertNotIn(b"secret", database_bytes)
        self.assertNotIn(b"must-not-be-stored", database_bytes)
        self.assertEqual(detail["query"], "signature=%2A%2A%2A")

    def test_request_summary_keeps_prompts_and_sanitized_reference_lists(self) -> None:
        call = captured(
            "/v1/video-generations/minimax-h3/jobs",
            {
                "prompts": ["第一段提示词", "第二段提示词"],
                "reference_image_urls": ["https://oss.example/a.png?signature=secret"],
                "reference_video_urls": ["https://oss.example/a.mp4?token=secret"],
                "reference_audio_urls": ["https://oss.example/a.mp3?expires=123"],
            },
            {"job_id": "job-1", "status": "queued"},
            status_code=202,
        )
        self.store.record(call)
        summary = self.store.call_detail(call.call_id)["request_summary"]
        self.assertEqual(summary["prompts"], ["第一段提示词", "第二段提示词"])
        self.assertNotIn("secret", json.dumps(summary, ensure_ascii=False))

    def test_pricing_rule_creates_immutable_ledger_for_sync_task(self) -> None:
        self.store.create_pricing_rule({
            "service": "tts",
            "operation": "synthesize",
            "unit_type": "1000_chars",
            "fixed_fee": "0.1",
            "unit_price": "2",
            "effective_from": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        })
        call = captured(
            "/v2/tts/speech",
            {"text": "字" * 1000, "language": "zh"},
            None,
            response_type="audio/wav",
        )
        self.store.record(call)

        tasks = self.store.list_tasks(page_size=10)
        self.assertEqual(tasks["total"], 1)
        self.assertEqual(tasks["items"][0]["status"], "succeeded")
        self.assertEqual(tasks["items"][0]["amount"], "2.100000")
        self.assertEqual(tasks["items"][0]["billing_status"], "billed")

    def test_async_status_queries_update_one_business_task(self) -> None:
        job_id = "e2aa9135-7d45-4fea-88a7-4fed9aca5064"
        create = captured(
            "/v1/lipsync/jobs",
            {"video_url": "https://cdn.example/video.mp4", "audio_url": "https://cdn.example/audio.mp3"},
            {"job_id": job_id, "state": "queued", "stage": "queued"},
            status_code=202,
        )
        self.store.record(create)
        status = captured(
            f"/v1/lipsync/jobs/{job_id}",
            {},
            {"job_id": job_id, "state": "completed", "stage": "completed", "video_duration_seconds": 20},
            method="GET",
        )
        self.store.record(status)

        tasks = self.store.list_tasks(page_size=10)
        self.assertEqual(tasks["total"], 1)
        self.assertEqual(tasks["items"][0]["status"], "completed")
        self.assertEqual(tasks["items"][0]["operation"], "create")
        detail = self.store.task_detail(tasks["items"][0]["id"])
        self.assertEqual(len(detail["calls"]), 2)
        self.assertEqual(len(detail["events"]), 2)
        overview = self.store.overview()
        self.assertEqual(overview["calls"], 2)
        self.assertEqual(overview["task_success_rate"], 1.0)

    def test_related_calls_use_exact_job_id_and_are_limited_to_ten(self) -> None:
        first_job = "e2aa9135-7d45-4fea-88a7-4fed9aca5064"
        second_job = "f2aa9135-7d45-4fea-88a7-4fed9aca5065"
        for job_id in (first_job, second_job):
            self.store.record(captured(
                "/v1/video-depth/jobs",
                {"source_uri": "https://cdn.example/video.mp4"},
                {"job_id": job_id, "status": "queued", "stage": "queued"},
                status_code=202,
            ))
        for _ in range(12):
            self.store.record(captured(
                f"/v1/video-depth/jobs/{first_job}",
                {},
                {"job_id": second_job, "status": "running", "stage": "inference"},
                method="GET",
            ))

        first_task = self.store.list_tasks(task_id=first_job, page_size=10)["items"][0]
        second_task = self.store.list_tasks(task_id=second_job, page_size=10)["items"][0]
        first_detail = self.store.task_detail(first_task["id"])
        second_detail = self.store.task_detail(second_task["id"])
        self.assertEqual(first_detail["calls_total"], 13)
        self.assertEqual(len(first_detail["calls"]), 10)
        self.assertTrue(all(first_job in call["path"] for call in first_detail["calls"]))
        self.assertEqual(second_detail["calls_total"], 1)

    def test_wait_and_upload_are_not_treated_as_job_ids(self) -> None:
        self.assertEqual(
            external_job_id("/v1/video-depth/jobs/wait", {"job_id": "real-job"}),
            "real-job",
        )
        self.assertIsNone(external_job_id("/v1/video-depth/jobs/wait", None))
        self.assertIsNone(external_job_id("/v1/lipsync/jobs/upload", None))

    def test_calls_and_tasks_are_paginated(self) -> None:
        for index in range(25):
            self.store.record(captured(
                "/v1/asr/transcriptions",
                {"file_url": f"https://cdn.example/{index}.wav", "language": "auto"},
                {"text": f"result-{index}"},
            ))

        calls = self.store.list_calls(page=2, page_size=10)
        tasks = self.store.list_tasks(page=3, page_size=10)
        self.assertEqual(calls["total"], 25)
        self.assertEqual(calls["page"], 2)
        self.assertEqual(len(calls["items"]), 10)
        self.assertEqual(tasks["total"], 25)
        self.assertEqual(tasks["page"], 3)
        self.assertEqual(len(tasks["items"]), 5)

    def test_daily_backup_and_health_are_available(self) -> None:
        self.assertTrue(self.store.backup_daily())
        self.assertFalse(self.store.backup_daily())
        backups = list((self.store.path.parent / "backups").glob("observability-*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.store.health()["status"], "ok")

    def test_real_analytics_separates_api_and_task_latency(self) -> None:
        job_id = "e2aa9135-7d45-4fea-88a7-4fed9aca5064"
        create = captured(
            "/v1/video-depth/jobs",
            {"source_uri": "https://cdn.example/video.mp4"},
            {"job_id": job_id, "status": "queued", "stage": "queued"},
            status_code=202,
        )
        self.store.record(create)
        status = captured(
            f"/v1/video-depth/jobs/{job_id}", {},
            {"job_id": job_id, "status": "succeeded", "stage": "completed"}, method="GET",
        )
        self.store.record(status)
        summary = self.store.analytics_summary(service="depth")
        self.assertEqual(summary["api"]["calls"], 2)
        self.assertEqual(summary["api"]["latency_ms"]["p95"], 25)
        self.assertEqual(summary["tasks"]["terminal"], 1)
        self.assertGreaterEqual(summary["tasks"]["latency_ms"]["p95"], 25)
        self.assertTrue(any(item["operation"] == "infer" for item in summary["endpoints"]))

    def test_empty_analytics_returns_null_percentiles(self) -> None:
        summary = self.store.analytics_summary(service="h3")
        self.assertIsNone(summary["api"]["latency_ms"]["p95"])
        self.assertIsNone(summary["tasks"]["latency_ms"]["mean"])

    def test_backfill_repairs_historical_async_duration(self) -> None:
        job_id = "e2aa9135-7d45-4fea-88a7-4fed9aca5064"
        call = captured(
            "/v1/video-generations/minimax-h3/jobs", {"duration_seconds": 5},
            {"job_id": job_id, "status": "queued"}, status_code=202,
        )
        self.store.record(call)
        created = datetime(2026, 9, 1, tzinfo=UTC)
        finished = created + timedelta(seconds=90)
        updated = self.store.backfill_task_timings("h3", [{
            "external_task_id": job_id, "status": "succeeded", "stage": "completed",
            "created_at": created.isoformat(), "started_at": (created + timedelta(seconds=10)).isoformat(),
            "finished_at": finished.isoformat(),
        }])
        self.assertEqual(updated, 1)
        task = self.store.list_tasks(service="h3", page_size=1)["items"][0]
        self.assertEqual(task["duration_ms"], 90_000)


if __name__ == "__main__":
    unittest.main()
