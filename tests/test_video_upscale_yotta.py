"""Yotta Starlight 作为视频超分 provider 的适配层。

只测纯函数与请求体形状：真实提交会按帧计费，不放进单测。
"""

from __future__ import annotations

import unittest
from unittest import mock


class ProviderRegistryTests(unittest.TestCase):
    def test_yotta_providers_join_the_whitelist(self):
        from control_plane.video_upscale_tasks import _all_providers, provider_order

        known = _all_providers()
        self.assertIn("starlight26", known)
        self.assertIn("starlightfast3", known)
        # 老的三条必须还在，否则存量请求会被 provider_order 静默丢掉
        for legacy in ("flashvsr", "flashvsr_v2", "seedvr2"):
            self.assertIn(legacy, known)

        # auto 只认配置里写了的，且不认识的名字被过滤掉
        self.assertEqual(provider_order("auto", "flashvsr_v2,starlight26"), ["flashvsr_v2", "starlight26"])
        self.assertEqual(provider_order("auto", "flashvsr_v2,bogus"), ["flashvsr_v2"])

    def test_yotta_is_not_in_the_default_auto_order(self):
        """按帧计费的第三方不能悄悄进 auto —— 那等于给存量调用换价目表。"""
        from control_plane.config import Settings
        import inspect

        default = inspect.signature(Settings).parameters["video_upscale_auto_provider_order"].default
        self.assertNotIn("starlight", default)

    def test_named_provider_passes_through_even_if_not_in_auto(self):
        from control_plane.video_upscale_tasks import provider_order

        self.assertEqual(provider_order("starlight26", "flashvsr_v2"), ["starlight26"])


class ResolutionBucketTests(unittest.TestCase):
    def test_input_bucket_by_short_side(self):
        from control_plane.video_upscale_tasks import _input_resolution_bucket

        self.assertEqual(_input_resolution_bucket(1280, 720), "720p")
        self.assertEqual(_input_resolution_bucket(1920, 1080), "1080p")
        self.assertEqual(_input_resolution_bucket(2560, 1440), "1440p")
        self.assertEqual(_input_resolution_bucket(3840, 2160), "4k")
        # 竖屏只看短边，不能拿宽比
        self.assertEqual(_input_resolution_bucket(720, 1280), "720p")

    def test_output_bucket_is_the_smallest_that_is_not_smaller(self):
        """max_resolution 是**最长边**；档位离散，取「不小于请求值的最小一档」。

        偏小等于花同样的钱拿更小的图；偏大的部分合并时会按 max_resolution 缩回去。
        """
        from control_plane.video_upscale_tasks import _output_resolution_bucket

        self.assertEqual(_output_resolution_bucket(480), "720p")
        self.assertEqual(_output_resolution_bucket(1280), "720p")
        self.assertEqual(_output_resolution_bucket(1920), "1080p")
        self.assertEqual(_output_resolution_bucket(2160), "1440p")
        self.assertEqual(_output_resolution_bucket(2560), "1440p")
        self.assertEqual(_output_resolution_bucket(3840), "4k")

    def test_aspect_ratio_snaps_to_a_supported_value(self):
        from control_plane.video_upscale_tasks import _aspect_ratio

        self.assertEqual(_aspect_ratio(1920, 1080), "16:9")
        self.assertEqual(_aspect_ratio(1080, 1920), "9:16")
        self.assertEqual(_aspect_ratio(1024, 1024), "1:1")


class YottaPayloadTests(unittest.TestCase):
    def _payload(self, **overrides):
        from control_plane.video_upscale_tasks import build_yotta_payload

        kwargs = dict(
            max_resolution=3840, size_bytes=3463682, duration_seconds=5.8333,
            frame_count=175, width=1280, height=720,
        )
        kwargs.update(overrides)
        return build_yotta_payload("starlight26", "https://x/a.mp4", **kwargs)

    def test_shape_matches_the_documented_contract(self):
        payload = self._payload()

        self.assertEqual(payload["model"], "slp-2.6")
        self.assertEqual(payload["providers"], ["topaz"])
        item = payload["parameters"]["reference_items"][0]
        self.assertEqual(item["type"], "video")
        self.assertEqual(item["url"], "https://x/a.mp4")
        self.assertEqual(item["size"], 3463682)
        self.assertEqual(item["frame_count"], 175)
        # 输入档看素材、输出档看请求 —— 写反了上游不报错，只是按错的档计费
        self.assertEqual(item["resolution"], "720p")
        self.assertEqual(payload["parameters"]["resolution"], "4k")
        self.assertEqual(payload["parameters"]["aspect_ratio"], "16:9")

    def test_duration_is_rounded_not_truncated_to_int(self):
        payload = self._payload(duration_seconds=5.8333)
        self.assertEqual(payload["parameters"]["reference_items"][0]["duration"], 5.833)

    def test_fast_model_uses_its_own_slug(self):
        from control_plane.video_upscale_tasks import build_yotta_payload

        payload = build_yotta_payload(
            "starlightfast3", "https://x/a.mp4", max_resolution=1920, size_bytes=1,
            duration_seconds=1.0, frame_count=30, width=1280, height=720,
        )
        self.assertEqual(payload["model"], "slf-3")
        self.assertEqual(payload["parameters"]["resolution"], "1080p")


class KeyPoolTests(unittest.TestCase):
    def _settings(self, keys: str, per_key: int = 2):
        import control_plane.video_upscale_tasks as tasks
        from pydantic import SecretStr

        fake = mock.Mock()
        fake.yotta_upscale_api_keys = SecretStr(keys) if keys else None
        fake.yotta_upscale_concurrency_per_key = per_key
        return mock.patch.object(tasks, "get_settings", return_value=fake), tasks

    def setUp(self):
        import control_plane.video_upscale_tasks as tasks

        tasks._YOTTA_KEY_SLOTS.clear()

    def test_splits_comma_separated_keys(self):
        patcher, tasks = self._settings(" aaa , bbb ,, ")
        with patcher:
            self.assertEqual(tasks._yotta_keys(), ["aaa", "bbb"])

    def test_missing_keys_raise_instead_of_falling_back(self):
        patcher, tasks = self._settings("")
        with patcher, self.assertRaises(tasks.ProviderFailure):
            tasks._acquire_yotta_slot()

    def test_per_key_limit_is_enforced_then_released(self):
        patcher, tasks = self._settings("aaa", per_key=1)
        with patcher:
            key, slot = tasks._acquire_yotta_slot()
            self.assertEqual(key, "aaa")
            # 唯一那一槽被占着：再取必须阻塞，用非阻塞探针验证
            self.assertFalse(tasks._YOTTA_KEY_SLOTS["aaa"].acquire(blocking=False))
            slot.release()
            self.assertTrue(tasks._YOTTA_KEY_SLOTS["aaa"].acquire(blocking=False))

    def test_rotation_spreads_across_accounts(self):
        patcher, tasks = self._settings("aaa,bbb", per_key=1)
        with patcher:
            first, s1 = tasks._acquire_yotta_slot()
            second, s2 = tasks._acquire_yotta_slot()
            self.assertNotEqual(first, second)
            s1.release()
            s2.release()


class PollingTests(unittest.TestCase):
    """轮询的状态机：completed 要取 output_url，failed 要立刻失败，不能一直等。"""

    def _run(self, responses):
        import control_plane.video_upscale_tasks as tasks

        calls = iter(responses)

        class FakeResponse:
            def __init__(self, body, status=200):
                self._body = body
                self.status_code = status

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return FakeResponse({"code": 10000, "data": {"request_id": "rid-1"}})

            def get(self, *a, **kw):
                return FakeResponse(next(calls))

        fake_settings = mock.Mock()
        fake_settings.yotta_upscale_base_url = "https://gateway/x"
        fake_settings.yotta_upscale_poll_seconds = 0
        fake_settings.video_upscale_request_timeout_seconds = 5
        fake_settings.video_upscale_provider_timeout_seconds = 30
        fake_settings.yotta_upscale_sharpness = 3.0
        fake_settings.yotta_upscale_rate_limit_retries = 0
        fake_settings.yotta_upscale_rate_limit_backoff_seconds = 0
        fake_settings.yotta_upscale_concurrency_per_key = 2
        fake_settings.yotta_upscale_api_keys = None

        request_data = {
            "source_uri": "https://x/a.mp4", "max_resolution": 2160,
            "segment_upload": "/nonexistent/segment.mp4",
            "segment_seconds": 1.0, "segment_frames": 30,
        }
        with mock.patch.object(tasks, "get_settings", return_value=fake_settings), \
             mock.patch.object(tasks.httpx, "Client", FakeClient), \
             mock.patch.object(tasks, "_media_metadata", return_value={"size": (1280, 720)}), \
             mock.patch.object(tasks.Path, "stat",
                               return_value=mock.Mock(st_size=3463682)), \
             mock.patch.object(tasks, "_acquire_yotta_slot",
                               return_value=("k", mock.Mock())):
            return tasks.run_yotta_provider("starlight26", request_data, lambda p: None)

    def test_completed_returns_output_url(self):
        url, rid = self._run([
            {"data": {"status": "processing"}},
            {"data": {"status": "completed", "output_url": "https://out/v.mp4"}},
        ])
        self.assertEqual(url, "https://out/v.mp4")
        self.assertEqual(rid, "rid-1")

    def test_failed_raises_immediately(self):
        import control_plane.video_upscale_tasks as tasks

        with self.assertRaises(tasks.ProviderFailure):
            self._run([{"data": {"status": "failed", "error": "boom"}}])

    def test_completed_without_url_is_a_failure(self):
        import control_plane.video_upscale_tasks as tasks

        with self.assertRaises(tasks.ProviderFailure):
            self._run([{"data": {"status": "completed"}}])


if __name__ == "__main__":
    unittest.main()
