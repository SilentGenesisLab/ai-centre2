from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import httpx

from control_plane.lipsync import LipSyncClient, LipSyncUpstreamError


class FakeClient:
    response = httpx.Response(202, json={"job_id": "job", "state": "queued"})
    last_request: dict[str, object] = {}

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        FakeClient.last_request = {"method": method, "url": url, **kwargs}
        return FakeClient.response


class LipSyncClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeClient.response = httpx.Response(
            202,
            json={"job_id": "job", "state": "queued"},
        )

    async def test_url_submit_uses_json_and_internal_bearer_token(self) -> None:
        client = LipSyncClient("http://musetalk/", "secret", 30)
        with patch("control_plane.lipsync.httpx.AsyncClient", FakeClient):
            result = await client.submit_urls(
                "https://cdn.example/face.mp4",
                "https://cdn.example/voice.wav",
                True,
            )

        self.assertEqual(result["state"], "queued")
        self.assertEqual(FakeClient.last_request["url"], "http://musetalk/v1/lipsync/jobs")
        self.assertEqual(
            FakeClient.last_request["headers"],
            {"Authorization": "Bearer secret"},
        )
        self.assertEqual(
            FakeClient.last_request["json"],
            {
                "video_url": "https://cdn.example/face.mp4",
                "audio_url": "https://cdn.example/voice.wav",
                "face_restore": True,
            },
        )

    async def test_upload_submit_uses_internal_upload_path(self) -> None:
        client = LipSyncClient("http://musetalk/", "secret", 30)
        with patch("control_plane.lipsync.httpx.AsyncClient", FakeClient):
            await client.submit_upload(
                "face.mp4",
                io.BytesIO(b"video"),
                "voice.wav",
                io.BytesIO(b"audio"),
                False,
            )

        self.assertEqual(
            FakeClient.last_request["url"],
            "http://musetalk/v1/lipsync/jobs/upload",
        )

    async def test_backend_validation_error_is_preserved(self) -> None:
        FakeClient.response = httpx.Response(415, json={"detail": "unsupported video file type"})
        client = LipSyncClient("http://musetalk", "secret", 30)
        with patch("control_plane.lipsync.httpx.AsyncClient", FakeClient):
            with self.assertRaises(LipSyncUpstreamError) as raised:
                await client.status("invalid")

        self.assertEqual(raised.exception.status_code, 415)
        self.assertEqual(raised.exception.detail, "unsupported video file type")

    async def test_list_jobs_forwards_filters(self) -> None:
        FakeClient.response = httpx.Response(200, json={"jobs": [], "total": 0})
        client = LipSyncClient("http://musetalk", "secret", 30)
        with patch("control_plane.lipsync.httpx.AsyncClient", FakeClient):
            result = await client.list_jobs(25, "completed")

        self.assertEqual(result["total"], 0)
        self.assertEqual(FakeClient.last_request["method"], "GET")
        self.assertEqual(FakeClient.last_request["url"], "http://musetalk/v1/lipsync/jobs")
        self.assertEqual(
            FakeClient.last_request["params"],
            {"limit": 25, "state": "completed"},
        )

    async def test_logs_forwards_allowlisted_query(self) -> None:
        FakeClient.response = httpx.Response(200, json={"log": "ready"})
        client = LipSyncClient("http://musetalk", "secret", 30)
        with patch("control_plane.lipsync.httpx.AsyncClient", FakeClient):
            result = await client.logs("job-id", "gfpgan", 120)

        self.assertEqual(result["log"], "ready")
        self.assertEqual(
            FakeClient.last_request["url"],
            "http://musetalk/v1/lipsync/jobs/job-id/logs",
        )
        self.assertEqual(
            FakeClient.last_request["params"],
            {"stage": "gfpgan", "tail": 120},
        )


if __name__ == "__main__":
    unittest.main()
