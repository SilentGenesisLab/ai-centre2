from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import HTTPException

from face_worker.api import (
    FaceMosaicJobRequest,
    create_job,
    create_job_and_wait,
)


class FaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = FaceMosaicJobRequest(
            source_uri="https://cdn.example/source.mp4"
        )

    def test_async_job_uses_high_priority(self) -> None:
        job = Mock(id="job-1")
        with patch("face_worker.api.process_face_mosaic.apply_async", return_value=job) as submit:
            result = create_job(self.request)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(submit.call_args.kwargs["queue"], "face_mosaic")
        self.assertEqual(submit.call_args.kwargs["priority"], 9)

    def test_wait_returns_worker_result_directly(self) -> None:
        expected = {
            "job_id": "job-1",
            "status": "succeeded",
            "video_url": "https://oss.example/result.mp4",
        }
        job = Mock(id="job-1")
        job.get.return_value = expected
        with (
            patch("face_worker.api.process_face_mosaic.apply_async", return_value=job),
            patch(
                "face_worker.api.get_settings",
                return_value=SimpleNamespace(face_wait_timeout_seconds=1800),
            ),
        ):
            result = create_job_and_wait(self.request)

        self.assertEqual(result, expected)
        job.get.assert_called_once_with(timeout=1800)

    def test_wait_timeout_leaves_job_running(self) -> None:
        job = Mock(id="job-1")
        job.get.side_effect = CeleryTimeoutError()
        with (
            patch("face_worker.api.process_face_mosaic.apply_async", return_value=job),
            patch(
                "face_worker.api.get_settings",
                return_value=SimpleNamespace(face_wait_timeout_seconds=1800),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                create_job_and_wait(self.request)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["job_id"], "job-1")

    def test_wait_worker_failure_is_gateway_error(self) -> None:
        job = Mock(id="job-1")
        job.get.side_effect = RuntimeError("private failure")
        with (
            patch("face_worker.api.process_face_mosaic.apply_async", return_value=job),
            patch(
                "face_worker.api.get_settings",
                return_value=SimpleNamespace(face_wait_timeout_seconds=1800),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                create_job_and_wait(self.request)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("private failure", str(raised.exception.detail))


if __name__ == "__main__":
    unittest.main()
