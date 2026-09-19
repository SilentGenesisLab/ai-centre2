from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import HTTPException
from pydantic import ValidationError

from control_plane.api import (
    AudioSeparationUrlJobRequest,
    cancel_audio_separation_job,
    create_audio_separation_job,
    create_audio_separation_job_and_wait,
    get_audio_separation_job,
)
from control_plane.audio_separation_jobs import AudioSeparationJobNotFound
from control_plane.observability import route_info, usage_from_payload


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


class FakeAudioSeparationJobs:
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


class AudioSeparationApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = AudioSeparationUrlJobRequest(
            source_uri="https://cdn.example/source.mp4"
        )

    async def test_async_submit_uses_production_defaults(self) -> None:
        jobs = FakeAudioSeparationJobs(Mock(id=JOB_ID))
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_audio_separation_jobs", return_value=jobs),
        ):
            result = await create_audio_separation_job(self.request)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["model"], "bandit-v2-multilingual")
        self.assertEqual(jobs.priorities, [5])
        self.assertEqual(jobs.requests[0]["filename_prefix"], "separated")

    def test_request_rejects_unknown_model_and_path_filename(self) -> None:
        with self.assertRaises(ValidationError):
            AudioSeparationUrlJobRequest(
                source_uri="https://cdn.example/source.mp4",
                model="mdx",
            )
        with self.assertRaises(ValidationError):
            AudioSeparationUrlJobRequest(
                source_uri="https://cdn.example/source.mp4",
                filename_prefix="../escape",
            )

    async def test_wait_uses_high_priority_and_returns_result(self) -> None:
        expected = {
            "job_id": JOB_ID,
            "status": "succeeded",
            "speech_url": "https://oss.example/speech.wav",
        }
        job = Mock(id=JOB_ID)
        job.get.return_value = expected
        jobs = FakeAudioSeparationJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_audio_separation_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(
                    audio_separation_wait_timeout_seconds=3600
                ),
            ),
        ):
            result = await create_audio_separation_job_and_wait(self.request)

        self.assertEqual(result, expected)
        self.assertEqual(jobs.priorities, [9])
        job.get.assert_called_once_with(timeout=3600)

    async def test_wait_timeout_keeps_job_for_polling(self) -> None:
        job = Mock(id=JOB_ID)
        job.get.side_effect = CeleryTimeoutError()
        jobs = FakeAudioSeparationJobs(job)
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_audio_separation_jobs", return_value=jobs),
            patch(
                "control_plane.api.get_settings",
                return_value=SimpleNamespace(
                    audio_separation_wait_timeout_seconds=3600
                ),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_audio_separation_job_and_wait(self.request)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["job_id"], JOB_ID)

    async def test_status_cancel_and_unknown_job(self) -> None:
        jobs = FakeAudioSeparationJobs(Mock(id=JOB_ID))
        with patch("control_plane.api.get_audio_separation_jobs", return_value=jobs):
            self.assertEqual(
                (await get_audio_separation_job(UUID(JOB_ID)))["status"], "queued"
            )
            self.assertEqual(
                (await cancel_audio_separation_job(UUID(JOB_ID)))["status"],
                "cancel_requested",
            )
        missing = Mock()
        missing.status.side_effect = AudioSeparationJobNotFound(JOB_ID)
        with patch("control_plane.api.get_audio_separation_jobs", return_value=missing):
            with self.assertRaises(HTTPException) as raised:
                await get_audio_separation_job(UUID(JOB_ID))
        self.assertEqual(raised.exception.status_code, 404)

    def test_routes_and_billing_are_observable(self) -> None:
        create = route_info("POST", "/v1/audio-separation/jobs")
        status_info = route_info("GET", f"/v1/audio-separation/jobs/{JOB_ID}")
        self.assertEqual(create.service, "separation")
        self.assertTrue(create.creates_task)
        self.assertTrue(status_info.status_query)
        quantity, unit = usage_from_payload(
            "separation", None, {"duration_seconds": 90}, {}
        )
        self.assertEqual((quantity, unit), (Decimal("1.5"), "audio_minute"))


if __name__ == "__main__":
    unittest.main()
