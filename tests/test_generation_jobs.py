from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.api_keys import ApiKeyStore
from control_plane.generation_jobs import GenerationJobClient


class GenerationJobClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Mock()
        self.client = GenerationJobClient(Mock(), self.store)

    @patch("control_plane.generation_jobs.GenerationJobClient._app")
    def test_cancel_queued_job_immediately(self, app: Mock) -> None:
        self.store.job.return_value = {"status": "queued"}

        result = self.client.cancel("job-1")

        app.return_value.control.revoke.assert_called_once_with(
            "job-1", terminate=False
        )
        self.store.update_job.assert_called_once_with(
            "job-1", status="cancelled", stage="cancelled"
        )
        self.assertEqual(result["status"], "cancelled")

    @patch("control_plane.generation_jobs.GenerationJobClient._app")
    def test_cancel_running_job_is_cooperative(self, app: Mock) -> None:
        self.store.job.return_value = {"status": "running"}

        result = self.client.cancel("job-2")

        app.return_value.control.revoke.assert_called_once_with(
            "job-2", terminate=False
        )
        self.store.update_job.assert_called_once_with(
            "job-2", status="cancel_requested", stage="cancel_requested"
        )
        self.assertEqual(result["status"], "cancel_requested")

    @patch("control_plane.generation_jobs.GenerationJobClient._app")
    def test_cancel_completed_job_is_noop(self, app: Mock) -> None:
        self.store.job.return_value = {"status": "succeeded"}

        result = self.client.cancel("job-3")

        app.return_value.control.revoke.assert_not_called()
        self.store.update_job.assert_not_called()
        self.assertEqual(result["status"], "succeeded")

    def _submit_charge(self, data: dict, service: str | None) -> dict:
        self.store.create_job.return_value = "job-x"
        with TemporaryDirectory() as directory:
            keys = ApiKeyStore(Path(directory) / "keys.db")
            self.client._key_store = keys
            with patch.object(GenerationJobClient, "_app"):
                if service is None:
                    self.client.submit(data)
                else:
                    self.client.submit(data, service)
            return keys.charges()["items"][0]

    def test_image_submit_is_billed_as_image_not_video(self) -> None:
        """图像与视频共用一个客户端，不显式传服务名就会按「5 秒视频」估价。"""
        row = self._submit_charge({"model": "moss-image", "image_size": "1K"}, "image_generation")
        self.assertEqual(row["service"], "image_generation")
        self.assertEqual(row["unit"], "image")
        self.assertEqual(row["amount_micro"], 0)

    def test_default_submit_is_billed_as_video(self) -> None:
        row = self._submit_charge({"model": "seedance-2.5", "duration_seconds": 5}, None)
        self.assertEqual(row["service"], "video_generation")
        self.assertEqual(row["amount_micro"], 8_870_970)


if __name__ == "__main__":
    unittest.main()
