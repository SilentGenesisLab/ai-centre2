from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import HTTPException
from pydantic import ValidationError

from control_plane.api import (
    DepthUrlJobRequest,
    cancel_depth_job,
    create_depth_job,
    create_depth_job_and_wait,
    get_depth_job,
)
from control_plane.depth_jobs import DepthJobNotFound
from control_plane.observability import route_info


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


class FakeDepthJobs:
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


class DepthApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = DepthUrlJobRequest(source_uri="https://cdn.example/source.mp4")

    async def test_async_submit_uses_da2_small_defaults(self) -> None:
        jobs = FakeDepthJobs(Mock(id=JOB_ID))
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_depth_jobs", return_value=jobs),
        ):
            result = await create_depth_job(self.request)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["version"], "da2")
        self.assertEqual(result["model"], "small")
        self.assertEqual(jobs.priorities, [5])
        self.assertEqual(jobs.requests[0]["input_size"], 518)
        self.assertEqual(jobs.requests[0]["max_resolution"], 960)

    def test_version_and_model_are_strict_enums(self) -> None:
        selected = DepthUrlJobRequest(
            source_uri="https://cdn.example/source.mp4",
            version="da3",
            model="base",
        )
        self.assertEqual((selected.version, selected.model), ("da3", "base"))
        with self.assertRaises(ValidationError):
            DepthUrlJobRequest(
                source_uri="https://cdn.example/source.mp4",
                version="da4",
            )

    async def test_wait_uses_high_priority_and_returns_result(self) -> None:
        expected = {
            "job_id": JOB_ID,
            "status": "succeeded",
            "version": "da3",
            "model": "base",
            "model_name": "Depth-Anything-3-Base",
            "video_url": "https://oss.example/depth.mp4",
        }
        job = Mock(id=JOB_ID)
        job.get.return_value = expected
        jobs = FakeDepthJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_depth_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(depth_wait_timeout_seconds=3600),
            ),
        ):
            result = await create_depth_job_and_wait(self.request)

        self.assertEqual(result, expected)
        self.assertEqual(jobs.priorities, [9])
        job.get.assert_called_once_with(timeout=3600)

    async def test_wait_timeout_keeps_job_for_polling(self) -> None:
        job = Mock(id=JOB_ID)
        job.get.side_effect = CeleryTimeoutError()
        jobs = FakeDepthJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_depth_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(depth_wait_timeout_seconds=3600),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_depth_job_and_wait(self.request)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["job_id"], JOB_ID)

    async def test_status_cancel_and_unknown_job(self) -> None:
        jobs = FakeDepthJobs(Mock(id=JOB_ID))
        with patch("control_plane.api.get_depth_jobs", return_value=jobs):
            self.assertEqual((await get_depth_job(UUID(JOB_ID)))["status"], "queued")
            self.assertEqual((await cancel_depth_job(UUID(JOB_ID)))["status"], "cancel_requested")
        missing = Mock()
        missing.status.side_effect = DepthJobNotFound(JOB_ID)
        with patch("control_plane.api.get_depth_jobs", return_value=missing):
            with self.assertRaises(HTTPException) as raised:
                await get_depth_job(UUID(JOB_ID))
        self.assertEqual(raised.exception.status_code, 404)

    def test_routes_are_observable(self) -> None:
        create = route_info("POST", "/v1/video-depth/jobs")
        status_info = route_info("GET", f"/v1/video-depth/jobs/{JOB_ID}")
        self.assertEqual(create.service, "depth")
        self.assertTrue(create.creates_task)
        self.assertTrue(status_info.status_query)


if __name__ == "__main__":
    unittest.main()
