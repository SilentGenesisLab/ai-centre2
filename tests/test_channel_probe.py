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


def wuyinkeji_channel(**overrides):
    channel = {
        "adapter": "wuyinkeji",
        "base_url": "https://api.wuyinkeji.com",
        "auth_type": "authorization",
        "credential": "wk-test-key",
        "timeout_seconds": 20,
    }
    channel.update(overrides)
    return channel


def probe_wuyinkeji(response: FakeResponse, **overrides):
    client = FakeClient(response)
    with patch("control_plane.generation_tasks.httpx.Client", return_value=client):
        ok, balance, error = probe_channel(wuyinkeji_channel(**overrides))
    return ok, balance, error, client


class WuyinkejiProbeTests(unittest.TestCase):
    """速创的探针跑在一个**格式合法但不存在**的任务号上。

    这个上游 HTTP 恒为 200，错误全在响应体的 `code` 字段里，所以判据只能是 body：
      code 400「错误的ID」    → 鉴权过了，只是查无此单 → 在线
      code 403「请求密钥KEY不正确！」→ 密钥不认 → 离线
    """

    def test_a_good_key_is_online_when_the_id_is_simply_unknown(self) -> None:
        ok, balance, error, _ = probe_wuyinkeji(FakeResponse(200, {"code": 400, "msg": "错误的ID", "data": []}))

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(balance["msg"], "错误的ID")

    def test_a_wrong_key_is_offline(self) -> None:
        # 上游用 403 的 **body code** 报密钥错误，HTTP 状态码仍是 200 —— 别去看状态码。
        ok, _, error, _ = probe_wuyinkeji(FakeResponse(200, {"code": 403, "msg": "请求密钥KEY不正确！"}))

        self.assertFalse(ok)
        self.assertIn("403", error)

    def test_the_probe_sends_the_raw_authorization_header(self) -> None:
        # 这一条是这个渠道最容易踩的坑：查询端点只认裸 Authorization，
        # 带 `Bearer ` 前缀或改用 X-API-Key 都会回 403（2026-10-08 实测）。
        _, _, _, client = probe_wuyinkeji(FakeResponse(200, {"code": 400, "msg": "错误的ID"}))

        _, headers = client.calls[0]
        self.assertEqual(headers.get("Authorization"), "wk-test-key")
        self.assertNotIn("Bearer", headers.get("Authorization", ""))
        self.assertNotIn("X-API-Key", headers)

    def test_the_probe_queries_the_detail_endpoint_with_a_well_formed_id(self) -> None:
        _, _, _, client = probe_wuyinkeji(FakeResponse(200, {"code": 400, "msg": "错误的ID"}))

        url, _ = client.calls[0]
        self.assertIn("/api/async/detail?id=image_", url)


def huanwangai_channel(**overrides):
    channel = {
        "adapter": "huanwangai",
        "base_url": "https://api.huanwangai.com",
        "auth_type": "authorization",
        "credential": "sk-mj-test",
        "timeout_seconds": 20,
    }
    channel.update(overrides)
    return channel


def probe_huanwangai(response: FakeResponse, **overrides):
    client = FakeClient(response)
    with patch("control_plane.generation_tasks.httpx.Client", return_value=client):
        ok, balance, error = probe_channel(huanwangai_channel(**overrides))
    return ok, balance, error, client


class HuanwangaiProbeTests(unittest.TestCase):
    """幻网AI（Midjourney）。探针查一个**格式合法但不存在**的任务号。

    这一家的鉴权发生在查库**之前**，所以三种结果可分（2026-10-08 实测）：
      401 → 没带 key 或 key 不认
      404 → 查无此任务，但**鉴权过了** —— 网关通 + key 有效
    """

    def test_401_is_offline(self) -> None:
        ok, _, error, _ = probe_huanwangai(FakeResponse(401, {"message": "unauthorized"}))

        self.assertFalse(ok)
        self.assertIn("401", error)

    def test_404_means_the_key_was_accepted(self) -> None:
        # 这是这条探针的关键：404 不是「渠道坏了」，是「查无此任务」，
        # 恰恰证明鉴权过了。当成离线会把一个正常的渠道误报掉。
        ok, balance, error, _ = probe_huanwangai(
            FakeResponse(404, {"title": "Not Found", "status": 404}))

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(balance["status_code"], 404)

    def test_200_is_online(self) -> None:
        ok, _, error, _ = probe_huanwangai(FakeResponse(200, {"status": "SUCCESS"}))

        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_a_5xx_is_offline(self) -> None:
        ok, _, error, _ = probe_huanwangai(FakeResponse(503, {}))

        self.assertFalse(ok)
        self.assertIn("503", error)

    def test_the_probe_queries_a_nonexistent_task_with_the_raw_header(self) -> None:
        _, _, _, client = probe_huanwangai(FakeResponse(404, {}))

        url, headers = client.calls[0]
        self.assertTrue(url.endswith("/mj/task/0/fetch"), url)
        self.assertEqual(headers.get("Authorization"), "sk-mj-test")
        self.assertNotIn("Bearer", headers.get("Authorization", ""))


if __name__ == "__main__":
    unittest.main()
