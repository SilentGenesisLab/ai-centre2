from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import HTTPException

from control_plane.api import (
    SceneUrlJobRequest,
    create_scene_job,
    create_scene_job_and_wait,
    get_scene_job,
)
from control_plane.scene_jobs import SceneJobNotFound


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


class FakeSceneJobs:
    def __init__(self, job: Mock) -> None:
        self.job = job
        self.priorities: list[int] = []

    def submit(self, _request_data, priority: int):
        self.priorities.append(priority)
        return self.job

    def status(self, job_id: str):
        return {"job_id": job_id, "status": "queued"}


class SceneApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = SceneUrlJobRequest(
            source_uri="https://cdn.example/source.mp4"
        )

    async def test_async_submit_uses_normal_priority(self) -> None:
        job = Mock(id=JOB_ID)
        jobs = FakeSceneJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_scene_jobs", return_value=jobs),
        ):
            result = await create_scene_job(self.request)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["priority"], 5)
        self.assertEqual(jobs.priorities, [5])

    async def test_wait_submit_uses_higher_priority_and_returns_result(self) -> None:
        expected = {
            "job_id": JOB_ID,
            "status": "succeeded",
            "scene_count": 1,
            "scenes": [
                {
                    "index": 1,
                    "video_url": "https://oss.example/scene-1.mp4",
                }
            ],
        }
        job = Mock(id=JOB_ID)
        job.get.return_value = expected
        jobs = FakeSceneJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_scene_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(scene_wait_timeout_seconds=1800),
            ),
        ):
            result = await create_scene_job_and_wait(self.request)

        self.assertEqual(result, expected)
        self.assertEqual(jobs.priorities, [9])
        job.get.assert_called_once_with(timeout=1800)

    async def test_wait_timeout_does_not_cancel_job(self) -> None:
        job = Mock(id=JOB_ID)
        job.get.side_effect = CeleryTimeoutError()
        jobs = FakeSceneJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_scene_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(scene_wait_timeout_seconds=1800),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_scene_job_and_wait(self.request)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["job_id"], JOB_ID)

    async def test_query_unknown_job_returns_404(self) -> None:
        jobs = Mock()
        jobs.status.side_effect = SceneJobNotFound(JOB_ID)
        with patch("control_plane.api.get_scene_jobs", return_value=jobs):
            with self.assertRaises(HTTPException) as raised:
                await get_scene_job(UUID(JOB_ID))

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
