from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from control_plane.llm_monitor import (
    DEGRADED,
    MAJOR_OUTAGE,
    OPERATIONAL,
    LlmMonitor,
    LlmProbeStore,
    parse_models,
)


def _delta_seconds(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class ParseModelsTests(unittest.TestCase):
    def test_splits_and_trims(self) -> None:
        self.assertEqual(parse_models(" a , b ,, c "), ("a", "b", "c"))

    def test_empty_falls_back_to_defaults(self) -> None:
        self.assertIn("gemini-4-argon", parse_models(""))
        self.assertIn("gpt-6.1-sol", parse_models(None))


class JudgeTests(unittest.TestCase):
    def test_normal_completion_is_a_success(self) -> None:
        ok, error = LlmMonitor._judge(FakeResponse(200, {
            "choices": [{"message": {"role": "assistant", "content": "Hi!"}}]
        }))
        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_reasoning_only_still_counts(self) -> None:
        ok, _ = LlmMonitor._judge(FakeResponse(200, {
            "choices": [{"message": {"content": "", "reasoning_content": "thinking"}}]
        }))
        self.assertTrue(ok)

    def test_http_error_is_a_failure(self) -> None:
        ok, error = LlmMonitor._judge(FakeResponse(403, {
            "error": {"message": "All available accounts exhausted"}
        }))
        self.assertFalse(ok)
        self.assertIn("403", error)

    def test_200_with_error_body_is_a_failure(self) -> None:
        ok, error = LlmMonitor._judge(FakeResponse(200, {
            "error": {"message": "model is not available"}
        }))
        self.assertFalse(ok)
        self.assertIn("上游错误", error)

    def test_missing_choices_is_a_failure(self) -> None:
        ok, error = LlmMonitor._judge(FakeResponse(200, {"object": "chat.completion"}))
        self.assertFalse(ok)
        self.assertIn("choices", error)

    def test_empty_answer_is_a_failure(self) -> None:
        ok, error = LlmMonitor._judge(FakeResponse(200, {
            "choices": [{"message": {"content": "   "}}]
        }))
        self.assertFalse(ok)
        self.assertIn("为空", error)


class SchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LlmProbeStore(Path(self.tmp.name) / "probe.db", ("m1",))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _row(self) -> dict:
        return self.store.targets()[0]

    def _record(self, success: bool):
        return self.store.record(
            "m1",
            success=success,
            latency_ms=12.5,
            status_code=200 if success else 500,
            error=None if success else "boom",
            interval_seconds=900,
            retry_seconds=60,
            failure_threshold=3,
        )

    def test_new_target_is_due_immediately(self) -> None:
        self.assertEqual(self.store.due_models(), ["m1"])

    def test_first_two_failures_retry_after_one_minute(self) -> None:
        self.assertIsNone(self._record(False))
        row = self._row()
        self.assertEqual(row["status"], DEGRADED)
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertAlmostEqual(
            _delta_seconds(row["last_checked_at"], row["next_check_at"]), 60, delta=2
        )

        self.assertIsNone(self._record(False))
        row = self._row()
        self.assertEqual(row["status"], DEGRADED)
        self.assertEqual(row["consecutive_failures"], 2)
        self.assertAlmostEqual(
            _delta_seconds(row["last_checked_at"], row["next_check_at"]), 60, delta=2
        )

    def test_third_failure_alerts_then_returns_to_the_normal_interval(self) -> None:
        self._record(False)
        self._record(False)
        transition = self._record(False)

        self.assertIsNotNone(transition)
        self.assertEqual(transition["kind"], "failure")
        self.assertEqual(transition["model"], "m1")
        self.assertEqual(transition["failures"], 3)

        row = self._row()
        self.assertEqual(row["status"], MAJOR_OUTAGE)
        self.assertAlmostEqual(
            _delta_seconds(row["last_checked_at"], row["next_check_at"]), 900, delta=2
        )

    def test_continued_failures_do_not_open_a_second_incident(self) -> None:
        for _ in range(3):
            self._record(False)
        self.assertIsNone(self._record(False))
        status = self.store.status()
        open_incidents = [i for i in status["incidents"] if i["status"] == "open"]
        self.assertEqual(len(open_incidents), 1)

    def test_recovery_after_outage_emits_a_recovery_transition(self) -> None:
        for _ in range(3):
            self._record(False)

        transition = self._record(True)
        self.assertEqual(transition["kind"], "recovery")
        row = self._row()
        self.assertEqual(row["status"], OPERATIONAL)
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertAlmostEqual(
            _delta_seconds(row["last_checked_at"], row["next_check_at"]), 900, delta=2
        )

    def test_recovery_from_degraded_is_silent(self) -> None:
        self._record(False)  # degraded, 没有告警
        self.assertIsNone(self._record(True))
        self.assertEqual(self._row()["status"], OPERATIONAL)

    def test_success_after_outage_is_recorded_once(self) -> None:
        for _ in range(3):
            self._record(False)
        self._record(True)
        self.assertIsNone(self._record(True))

    def test_status_exposes_history_and_availability(self) -> None:
        self._record(True)
        self._record(False)
        status = self.store.status()
        item = status["items"][0]
        self.assertEqual(item["model"], "m1")
        self.assertEqual(item["sample_count"], 2)
        self.assertEqual(item["availability"], 50.0)
        self.assertEqual([point["ok"] for point in item["history"]], [True, False])


if __name__ == "__main__":
    unittest.main()
