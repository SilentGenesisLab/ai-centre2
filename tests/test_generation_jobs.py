from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("SERVICE_TOKEN", "test")

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


if __name__ == "__main__":
    unittest.main()
