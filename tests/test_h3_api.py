from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.api import (
    H3VideoJobRequest,
    create_h3_job,
    get_h3_queue_position,
    get_h3_worker_status,
)


class H3ApiTests(unittest.IsolatedAsyncioTestCase):
    def test_request_mode_prompt_count_and_resolution(self) -> None:
        with self.assertRaises(ValidationError):
            H3VideoJobRequest(
                reference_video_urls=["https://cdn.example/video.mp4"],
                segment_mode="two_part",
                prompts=["one", "two"],
            )
        with self.assertRaises(ValidationError):
            H3VideoJobRequest(segment_mode="single", prompts=["one"])
        with self.assertRaises(ValidationError):
            H3VideoJobRequest(
                reference_video_urls=["https://cdn.example/video.mp4"], prompt="one", width=1344, height=1344,
            )
        image_audio = H3VideoJobRequest(
            reference_image_urls=["https://cdn.example/image.png"],
            reference_audio_urls=["https://cdn.example/audio.mp3"],
            prompt="one", resolution="1080p", aspect_ratio="16:9", duration_seconds=8,
        )
        self.assertEqual(image_audio.reference_video_urls, [])
        text_only = H3VideoJobRequest(prompt="one")
        self.assertEqual(text_only.quality, "medium")
        self.assertEqual(text_only.priority, 500)
        self.assertEqual(text_only.reference_video_urls, [])
        self.assertEqual(text_only.reference_image_urls, [])
        self.assertEqual(text_only.reference_audio_urls, [])
        with self.assertRaises(ValidationError):
            H3VideoJobRequest(prompt="one", priority=0)
        with self.assertRaises(ValidationError):
            H3VideoJobRequest(prompt="one", priority=1001)
        self.assertEqual(H3VideoJobRequest(prompt="one", quality="midia").quality, "medium")
        with self.assertRaises(ValidationError):
            H3VideoJobRequest(prompt="one", quality="ultra")

    async def test_submit_returns_only_public_job_fields(self) -> None:
        request = H3VideoJobRequest(
            reference_video_urls=["https://cdn.example/video.mp4"],
            prompt="exact prompt",
            priority=900,
        )
        jobs = Mock()
        jobs.submit.return_value = Mock(id="4c39cf01-9893-436e-9378-1be045d98f64")
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_h3_jobs", return_value=jobs),
        ):
            result = await create_h3_job(request)
        self.assertEqual(result["status"], "queued")
        self.assertNotIn("worker_url", result)
        self.assertNotIn("prompt_id", result)
        self.assertEqual(result["priority"], 900)
        self.assertEqual(result["quality"], "medium")
        self.assertIn("queue-position", result["queue_position_url"])
        self.assertEqual(jobs.submit.call_args.args[0]["prompts"], ["exact prompt"])
        self.assertEqual(jobs.submit.call_args.args[0]["priority"], 900)

    async def test_text_only_submit_is_accepted(self) -> None:
        request = H3VideoJobRequest(prompt="text only prompt", duration_seconds=5)
        jobs = Mock()
        jobs.submit.return_value = Mock(id="7c39cf01-9893-436e-9378-1be045d98f64")
        with patch("control_plane.api.get_h3_jobs", return_value=jobs):
            result = await create_h3_job(request)
        self.assertEqual(result["status"], "queued")
        payload = jobs.submit.call_args.args[0]
        self.assertEqual(payload["reference_video_urls"], [])
        self.assertEqual(payload["reference_image_urls"], [])
        self.assertEqual(payload["reference_audio_urls"], [])

    def test_legacy_single_urls_normalize_to_lists(self) -> None:
        request = H3VideoJobRequest(reference_video_url="https://cdn.example/video.mp4", prompt="one")
        self.assertEqual(request.reference_video_urls, ["https://cdn.example/video.mp4"])

    async def test_queue_position_and_worker_status_queries(self) -> None:
        jobs = Mock()
        jobs.queue_position.return_value = {
            "job_id": "7c39cf01-9893-436e-9378-1be045d98f64",
            "queued": True,
            "ahead_count": 2,
            "position": 3,
        }
        jobs.worker_summary.return_value = {"online": 2, "idle": 1, "busy": 1, "disabled": 3}
        with patch("control_plane.api.get_h3_jobs", return_value=jobs):
            queue = await get_h3_queue_position("7c39cf01-9893-436e-9378-1be045d98f64")
            workers = await get_h3_worker_status()
        self.assertEqual(queue["ahead_count"], 2)
        self.assertEqual(workers["busy"], 1)


if __name__ == "__main__":
    unittest.main()
