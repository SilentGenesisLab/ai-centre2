from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.generation_tasks import probe_channel


class FakeResponse:
    def __init__(self, status_code: int, body) -> None:
        self.status_code, self._body = status_code, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    """记下请求过的 URL 与请求头，回一份预设响应。"""

    def __init__(self, response: FakeResponse) -> None:
        self.response, self.calls = response, []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get(self, url: str, headers=None, **kwargs) -> FakeResponse:
        self.calls.append((url, headers or {}))
        return self.response

    def post(self, url: str, headers=None, **kwargs) -> FakeResponse:
        self.calls.append((url, headers or {}))
        return self.response


def jmapi_channel(**overrides):
    channel = {
        "adapter": "jmapi",
        "base_url": "https://chorify3.sligenai.cn",
        "auth_type": "x-api-key",
        "credential": "sk-jm-chorify-test",
        "timeout_seconds": 20,
    }
    channel.update(overrides)
    return channel


def probe(response: FakeResponse, **overrides):
    client = FakeClient(response)
    with patch("control_plane.generation_tasks.httpx.Client", return_value=client):
        ok, balance, error = probe_channel(jmapi_channel(**overrides))
    return ok, balance, error, client


class JmapiProbeTests(unittest.TestCase):
    """2026-10-01 起 jmapi 的生成类端点要 X-API-Key，探针必须真的把这个 key 带上并验它。

    探针本身只用 `/v1/keys`（只读、不消耗积分）：生成类端点会花钱，不能拿来当探针。
    """

    def test_the_probe_hits_the_read_only_key_endpoint_with_the_key(self) -> None:
        ok, _, error, client = probe(FakeResponse(200, {"count": 5}))

        self.assertTrue(ok)
        self.assertIsNone(error)
        url, headers = client.calls[0]
        self.assertEqual(url, "https://chorify3.sligenai.cn/jmapi/v1/keys")
        self.assertEqual(headers["X-API-Key"], "sk-jm-chorify-test")

    def test_a_missing_key_is_offline_and_sends_no_header(self) -> None:
        # 没配 key 就必须报离线。原来的 `/jmapi/status` 探针不带 key 也回 200，
        # 于是「生成类 401 → 兜底换到 libtv → 作业照样成功」这条路没人看得出来。
        ok, _, error, client = probe(FakeResponse(401, {"detail": "缺少或无效的 API Key"}), credential=None)

        self.assertFalse(ok)
        self.assertIn("401", error)
        self.assertNotIn("X-API-Key", client.calls[0][1])

    def test_a_wrong_key_is_offline(self) -> None:
        ok, _, error, _ = probe(FakeResponse(401, {"detail": "缺少或无效的 API Key"}))

        self.assertFalse(ok)
        self.assertIn("401", error)

    def test_a_disabled_key_is_offline(self) -> None:
        ok, _, error, _ = probe(FakeResponse(403, {"detail": "API Key「chorify」已被停用，请联系管理员"}))

        self.assertFalse(ok)
        self.assertIn("停用", error)

    def test_a_plain_user_key_is_online(self) -> None:
        # 403 +「需 role=admin」= key 认得出、只是没有管理权限 —— 生成类本来就只认普通 key。
        ok, balance, error, _ = probe(FakeResponse(403, {"detail": "该 API Key 无管理权限（需 role=admin）"}))

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(balance["status_code"], 403)

    def test_a_server_error_is_offline(self) -> None:
        ok, _, error, _ = probe(FakeResponse(500, {"detail": "boom"}))

        self.assertFalse(ok)
        self.assertIn("500", error)

    def test_a_non_json_body_still_counts_as_online(self) -> None:
        ok, _, error, _ = probe(FakeResponse(200, ValueError("not json")))

        self.assertTrue(ok)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
