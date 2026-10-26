from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from fastapi.testclient import TestClient

from control_plane.ai_capabilities import CapabilityStore

AUTH = {"Authorization": "Bearer test"}


class JobLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = CapabilityStore(Path(tempfile.mkdtemp()) / "cap.db", "unit-secret")
        self._patch = patch("control_plane.api.get_capability_store", return_value=self.store)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def _channel_id(self, code: str) -> str:
        return next(channel["id"] for channel in self.store.channels() if channel["code"] == code)

    def _job(self, *, model: str = "gpt-image-2", status: str = "succeeded",
             channel: str | None = "grsai", error: str | None = None,
             attempts: list[dict] | None = None, elapsed: float = 12.5) -> str:
        job_id = self.store.create_job(model, "auto", {
            "model": model,
            "prompt": "一只猫",
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
        })
        values: dict = {
            "status": status,
            "stage": status,
            "elapsed_seconds": elapsed,
            "result_urls_json": json.dumps(["https://cdn.test/a.png"] if status == "succeeded" else []),
            "attempts_json": json.dumps(attempts or [], ensure_ascii=False),
        }
        if error:
            values["error"] = error
        if channel:
            values["channel_id"] = self._channel_id(channel)
        self.store.update_job(job_id, **values)
        return job_id

    def _client(self) -> TestClient:
        from control_plane.api import app

        return TestClient(app)

    # --- store -----------------------------------------------------------

    def test_attempts_round_trip(self):
        attempts = [
            {"channel": "jmapi", "outcome": "failed", "error": "upstream HTTP 502", "elapsed_ms": 1200, "at": "2026-10-21T21:00:00+00:00"},
            {"channel": "libtv", "outcome": "success", "error": None, "elapsed_ms": 60000, "at": "2026-10-21T21:01:00+00:00"},
        ]
        job_id = self._job(attempts=attempts, channel="libtv")
        stored = self.store.job(job_id)["attempts"]
        self.assertEqual([item["channel"] for item in stored], ["jmapi", "libtv"])
        self.assertEqual(stored[0]["outcome"], "failed")
        self.assertEqual(stored[1]["outcome"], "success")

    def test_query_filters_and_pagination(self):
        self._job(model="gpt-image-2")
        self._job(model="gpt-image-2", status="failed", error="upstream HTTP 500")
        self._job(model="seedance-2.0", status="failed", channel="jmapi", error="upstream HTTP 502")

        videos = self.store.query_job_logs(capability_type="video_generation")
        self.assertEqual(videos["total"], 1)
        self.assertEqual(videos["items"][0]["model"], "seedance-2.0")

        failures = self.store.query_job_logs(status="failed")
        self.assertEqual(failures["total"], 2)

        first_page = self.store.query_job_logs(page=1, page_size=2)
        second_page = self.store.query_job_logs(page=2, page_size=2)
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(len(first_page["items"]), 2)
        self.assertEqual(len(second_page["items"]), 1)
        self.assertNotEqual(first_page["items"][0]["id"], second_page["items"][0]["id"])

    def test_stats_count_attempts_and_failure_reasons(self):
        self._job(channel="libtv", attempts=[
            {"channel": "jmapi", "outcome": "failed", "error": "upstream HTTP 502", "elapsed_ms": 800},
            {"channel": "libtv", "outcome": "success", "error": None, "elapsed_ms": 30000},
        ])
        self._job(status="failed", channel="jmapi", error="jmapi:upstream HTTP 502")

        stats = self.store.job_log_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["success_rate"], 0.5)

        channels = {item["channel"]: item for item in stats["by_channel"]}
        self.assertEqual(channels["libtv"]["attempts"], 1)
        self.assertEqual(channels["libtv"]["succeeded"], 1)
        self.assertEqual(channels["jmapi"]["attempts"], 2)
        self.assertEqual(channels["jmapi"]["failed"], 2)
        self.assertEqual(channels["jmapi"]["success_rate"], 0.0)
        self.assertEqual(channels["libtv"]["avg_elapsed_ms"], 30000)

        self.assertTrue(stats["failure_reasons"])
        self.assertEqual(stats["failure_reasons"][0]["count"], 2)
        self.assertIn("upstream HTTP 502", stats["failure_reasons"][0]["reason"])

    # --- api -------------------------------------------------------------

    def test_list_and_stats_endpoints(self):
        self._job(channel="libtv", attempts=[
            {"channel": "jmapi", "outcome": "failed", "error": "upstream HTTP 502", "elapsed_ms": 800},
            {"channel": "libtv", "outcome": "success", "error": None, "elapsed_ms": 30000},
        ])
        client = self._client()

        listed = client.get("/internal/admin/ai-capabilities/job-logs", headers=AUTH)
        self.assertEqual(listed.status_code, 200)
        payload = listed.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["attempts"][0]["channel"], "jmapi")
        self.assertNotIn("request", payload["items"][0])

        filtered = client.get(
            "/internal/admin/ai-capabilities/job-logs",
            params={"capability_type": "video_generation"},
            headers=AUTH,
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 0)

        stats = client.get("/internal/admin/ai-capabilities/job-logs/stats", headers=AUTH)
        self.assertEqual(stats.status_code, 200)
        self.assertEqual(stats.json()["succeeded"], 1)
        self.assertEqual(stats.json()["by_channel"][0]["channel"], "jmapi")

    def test_detail_endpoint_and_missing_job(self):
        job_id = self._job(attempts=[{"channel": "grsai", "outcome": "success"}])
        client = self._client()

        detail = client.get(f"/internal/admin/ai-capabilities/job-logs/{job_id}", headers=AUTH)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["request"]["prompt"], "一只猫")
        self.assertEqual(detail.json()["attempts"][0]["channel"], "grsai")

        missing = client.get(
            "/internal/admin/ai-capabilities/job-logs/00000000-0000-4000-8000-000000000000",
            headers=AUTH,
        )
        self.assertEqual(missing.status_code, 404)

    def test_endpoints_require_service_token(self):
        client = self._client()
        self.assertEqual(client.get("/internal/admin/ai-capabilities/job-logs").status_code, 401)
        self.assertEqual(client.get("/internal/admin/ai-capabilities/job-logs/stats").status_code, 401)


if __name__ == "__main__":
    unittest.main()
