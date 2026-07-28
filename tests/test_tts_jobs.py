from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.tts.jobs import TTSJobClient
from control_plane.tts.schemas import TTSJobRequest, TTSJobStatus


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)


class TTSJobClientTests(unittest.TestCase):
    def test_idempotency_key_returns_the_same_job_without_resubmitting(self) -> None:
        client = TTSJobClient.__new__(TTSJobClient)
        client.settings = SimpleNamespace(tts_result_expires_seconds=3600)
        client.redis = FakeRedis()
        client.status = Mock(
            return_value=TTSJobStatus(job_id="ignored", status="queued")
        )
        celery = Mock()
        request = TTSJobRequest(
            idempotency_key="video-1-segment-1",
            text="hola",
            language="es-MX",
        )

        with patch(
            "control_plane.tts.jobs._get_celery_app",
            return_value=celery,
        ):
            first = client.submit(request)
            second = client.submit(request)

        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(celery.send_task.call_count, 1)


if __name__ == "__main__":
    unittest.main()

