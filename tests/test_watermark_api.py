from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import HTTPException

from control_plane.api import (
    WatermarkUrlJobRequest,
    cancel_watermark_job,
    create_watermark_job,
    create_watermark_job_and_wait,
    get_watermark_job,
)
from control_plane.watermark_jobs import WatermarkJobNotFound
from control_plane.observability import route_info


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


class FakeWatermarkJobs:
    def __init__(self, job: Mock) -> None:
        self.job = job
        self.priorities: list[int] = []
        self.requests: list[dict] = []

    def submit(self, request_data, priority: int):
        self.requests.append(request_data)
        self.priorities.append(priority)
        return self.job

    def status(self, job_id: str):
        return {"job_id": job_id, "status": "queued"}

    def cancel(self, job_id: str):
        return {"job_id": job_id, "status": "cancel_requested"}


class WatermarkApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = WatermarkUrlJobRequest(
            source_uri="https://cdn.example/source.mp4",
            mode="intensive",
        )

    async def test_async_submit_uses_normal_priority_and_default_cleanup(self) -> None:
        job = Mock(id=JOB_ID)
        jobs = FakeWatermarkJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_watermark_jobs", return_value=jobs),
        ):
            result = await create_watermark_job(self.request)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["priority"], 5)
        self.assertEqual(jobs.priorities, [5])
        self.assertFalse(jobs.requests[0]["keep_intermediates"])

    async def test_wait_uses_high_priority_and_returns_result(self) -> None:
        expected = {
            "job_id": JOB_ID,
            "status": "succeeded",
            "mode": "intensive",
            "video_url": "https://oss.example/result.mp4",
        }
        job = Mock(id=JOB_ID)
        job.get.return_value = expected
        jobs = FakeWatermarkJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_watermark_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(watermark_wait_timeout_seconds=3600),
            ),
        ):
            result = await create_watermark_job_and_wait(self.request)

        self.assertEqual(result, expected)
        self.assertEqual(jobs.priorities, [9])
        job.get.assert_called_once_with(timeout=3600)

    async def test_wait_timeout_keeps_job_available_for_polling(self) -> None:
        job = Mock(id=JOB_ID)
        job.get.side_effect = CeleryTimeoutError()
        jobs = FakeWatermarkJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_watermark_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(watermark_wait_timeout_seconds=3600),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_watermark_job_and_wait(self.request)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["job_id"], JOB_ID)

    async def test_status_and_cancel_use_the_same_job(self) -> None:
        jobs = FakeWatermarkJobs(Mock(id=JOB_ID))
        with patch("control_plane.api.get_watermark_jobs", return_value=jobs):
            status_result = await get_watermark_job(UUID(JOB_ID))
            cancel_result = await cancel_watermark_job(UUID(JOB_ID))

        self.assertEqual(status_result["status"], "queued")
        self.assertEqual(cancel_result["status"], "cancel_requested")

    async def test_unknown_job_returns_404(self) -> None:
        jobs = Mock()
        jobs.status.side_effect = WatermarkJobNotFound(JOB_ID)
        with patch("control_plane.api.get_watermark_jobs", return_value=jobs):
            with self.assertRaises(HTTPException) as raised:
                await get_watermark_job(UUID(JOB_ID))

        self.assertEqual(raised.exception.status_code, 404)

    def test_routes_are_registered_for_observability(self) -> None:
        create = route_info("POST", "/v1/watermark-removal/jobs")
        status_info = route_info("GET", f"/v1/watermark-removal/jobs/{JOB_ID}")

        self.assertEqual(create.service, "watermark")
        self.assertTrue(create.creates_task)
        self.assertEqual(status_info.service, "watermark")
        self.assertTrue(status_info.status_query)


if __name__ == "__main__":
    unittest.main()
