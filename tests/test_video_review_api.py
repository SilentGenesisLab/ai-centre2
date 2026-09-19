from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi import HTTPException

from control_plane.api import VideoReviewUrlJobRequest, create_video_review_job, get_video_review_job
from control_plane.video_review_jobs import VideoReviewJobNotFound


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


class FakeReviewJobs:
    def __init__(self, job: Mock) -> None:
        self.job = job
        self.priorities: list[int] = []

    def submit(self, _request_data, priority: int):
        self.priorities.append(priority)
        return self.job

    def status(self, job_id: str):
        return {"job_id": job_id, "status": "queued"}


class VideoReviewApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = VideoReviewUrlJobRequest(video_url="https://cdn.example/ad.mp4")

    async def test_default_is_reference_free(self) -> None:
        jobs = FakeReviewJobs(Mock(id=JOB_ID))
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_video_review_jobs", return_value=jobs),
        ):
            result = await create_video_review_job(self.request)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(jobs.priorities, [5])

    async def test_reference_assets_require_explicit_continuity_mode(self) -> None:
        request = VideoReviewUrlJobRequest(
            video_url="https://cdn.example/ad.mp4",
            reference_assets=[{"url": "https://cdn.example/ref.mp4", "kind": "video"}],
        )
        with self.assertRaises(HTTPException) as raised:
            await create_video_review_job(request)
        self.assertEqual(raised.exception.status_code, 422)

    async def test_unknown_job_returns_404(self) -> None:
        jobs = Mock()
        jobs.status.side_effect = VideoReviewJobNotFound(JOB_ID)
        with patch("control_plane.api.get_video_review_jobs", return_value=jobs):
            with self.assertRaises(HTTPException) as raised:
                await get_video_review_job(UUID(JOB_ID))
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

