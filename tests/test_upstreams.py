from __future__ import annotations

import unittest
from unittest.mock import patch

from control_plane.upstreams import AudioUpstreams


class FakeResponse:
    content = b"RIFF"
    headers = {"content-type": "audio/wav"}

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    last_url = ""
    last_json: dict[str, object] = {}

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, json: dict[str, object]) -> FakeResponse:
        FakeClient.last_url = url
        FakeClient.last_json = json
        return FakeResponse()


class AudioUpstreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_tts_uses_tts_endpoint(self) -> None:
        upstreams = AudioUpstreams("http://asr", "http://tts", 30)
        with patch("control_plane.upstreams.httpx.AsyncClient", FakeClient):
            await upstreams.synthesize(
                {
                    "text": "hello",
                    "cfg_value": 2.0,
                    "inference_timesteps": 10,
                }
            )

        self.assertEqual(FakeClient.last_url, "http://tts/tts")
        self.assertEqual(FakeClient.last_json["text"], "hello")

    async def test_reference_audio_uses_clone_endpoint(self) -> None:
        upstreams = AudioUpstreams("http://asr", "http://tts", 30)
        with patch("control_plane.upstreams.httpx.AsyncClient", FakeClient):
            await upstreams.synthesize(
                {
                    "text": "hello",
                    "reference_wav_path": "/tmp/reference.wav",
                }
            )

        self.assertEqual(FakeClient.last_url, "http://tts/clone_path")
        self.assertEqual(
            FakeClient.last_json["reference_wav_path"],
            "/tmp/reference.wav",
        )


if __name__ == "__main__":
    unittest.main()

