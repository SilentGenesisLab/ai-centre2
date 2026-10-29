"""失败作业的 error 字段必须带真实原因，而不是泛化的一句话。

原来 `job.state == "FAILURE"` 被固定投影成「video upscale failed」，
于是「分辨率超上限」「帧账错配」这些能直接修的问题，在调用方看来毫无区别。
"""

from __future__ import annotations

import unittest
from unittest import mock


class _FakeJob:
    def __init__(self, info):
        self.info = info


class FailureTextTests(unittest.TestCase):
    def _text(self, info, fallback="job failed"):
        from control_plane.generation_jobs import failure_text

        return failure_text(_FakeJob(info), fallback)

    def test_uses_the_exception_message(self):
        err = RuntimeError("segment 1 failed: 输出边长 3840 超出上限(最大 2560)")
        self.assertIn("3840", self._text(err))
        self.assertIn("2560", self._text(err))

    def test_collapses_newlines_and_truncates(self):
        err = RuntimeError("第一行\n\n第二行\t第三行" + "x" * 2000)
        text = self._text(err)
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), 600)

    def test_falls_back_when_there_is_nothing_useful(self):
        self.assertEqual(self._text(None, "video upscale failed"), "video upscale failed")
        self.assertEqual(self._text("", "video upscale failed"), "video upscale failed")


class JobStatusSurfacesItTests(unittest.TestCase):
    def _client(self, state, info):
        import control_plane.video_upscale_jobs as mod

        client = mod.VideoUpscaleJobClient.__new__(mod.VideoUpscaleJobClient)
        client.redis = mock.Mock()
        client.redis.exists.return_value = 1

        class FakeAsync:
            def __init__(self, *a, **kw):
                self.state = state
                self.info = info
                self.result = None

        with mock.patch.object(mod, "AsyncResult", FakeAsync), \
             mock.patch.object(mod.VideoUpscaleJobClient, "_celery_app", return_value=None):
            return client.status("job-1")

    def test_failure_carries_the_reason(self):
        result = self._client("FAILURE", RuntimeError("请求的输出边长 3840 超出可用超分渠道的上限"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("3840", result["error"])

    def test_generic_message_is_still_there_as_a_floor(self):
        result = self._client("FAILURE", None)
        self.assertEqual(result["error"], "video upscale failed")


if __name__ == "__main__":
    unittest.main()
