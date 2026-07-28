from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

import httpx

from control_plane.tts.base import TransientTTSProviderError
from control_plane.tts.providers.common import raise_for_provider_status
from control_plane.tts.providers.doubao import DoubaoProvider
from control_plane.tts.providers.elevenlabs import ElevenLabsProvider
from control_plane.tts.schemas import TTSSpeechRequest


class FakeResponse:
    def __init__(self, content=b"audio", json_body=None, headers=None) -> None:
        self.content = content
        self._json_body = json_body
        self.headers = headers or {}
        self.status_code = 200
        self.is_success = True
        self.text = ""

    def json(self):
        return self._json_body


class FakeClient:
    response = FakeResponse()
    last_url = ""
    last_kwargs = {}

    def __init__(self, **_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url, **kwargs):
        FakeClient.last_url = url
        FakeClient.last_kwargs = kwargs
        return FakeClient.response


class TTSProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_doubao_maps_standard_request_and_decodes_audio(self) -> None:
        FakeClient.response = FakeResponse(
            json_body={
                "reqid": "provider-request",
                "code": 3000,
                "message": "Success",
                "data": base64.b64encode(b"mp3-audio").decode(),
                "addition": {"duration": "1234"},
            }
        )
        provider = DoubaoProvider(
            "https://doubao.example/tts",
            "app",
            "token",
            "volcano_tts",
            30,
            True,
        )

        with patch(
            "control_plane.tts.providers.doubao.httpx.AsyncClient",
            FakeClient,
        ):
            result = await provider.synthesize(
                TTSSpeechRequest(
                    text="สวัสดี",
                    language="th-TH",
                ),
                {"voice_type": "thai-voice"},
            )

        self.assertEqual(result.audio, b"mp3-audio")
        self.assertEqual(result.provider_duration_ms, 1234)
        payload = FakeClient.last_kwargs["json"]
        self.assertEqual(payload["audio"]["voice_type"], "thai-voice")
        self.assertEqual(
            FakeClient.last_kwargs["headers"]["Authorization"],
            "Bearer;token",
        )

    async def test_elevenlabs_maps_voice_and_language(self) -> None:
        FakeClient.response = FakeResponse(
            content=b"mp3-audio",
            headers={
                "content-type": "audio/mpeg",
                "request-id": "request-1",
            },
        )
        provider = ElevenLabsProvider(
            "https://api.elevenlabs.io",
            "key",
            "eleven_multilingual_v2",
            "mp3_44100_128",
            30,
            True,
        )

        with patch(
            "control_plane.tts.providers.elevenlabs.httpx.AsyncClient",
            FakeClient,
        ):
            result = await provider.synthesize(
                TTSSpeechRequest(
                    text="Hola",
                    language="es-MX",
                ),
                {"voice_id": "voice/1"},
            )

        self.assertTrue(FakeClient.last_url.endswith("/voice%2F1"))
        self.assertEqual(FakeClient.last_kwargs["json"]["language_code"], "es")
        self.assertEqual(
            FakeClient.last_kwargs["headers"]["xi-api-key"],
            "key",
        )
        self.assertEqual(result.provider_request_id, "request-1")


class ProviderErrorTests(unittest.TestCase):
    def test_server_error_does_not_expose_upstream_traceback(self) -> None:
        response = httpx.Response(
            500,
            json={
                "detail": "Traceback from /home/private/model.py",
                "message": "internal CUDA failure",
            },
            headers={"x-request-id": "provider-request-1"},
        )

        with self.assertRaises(TransientTTSProviderError) as raised:
            raise_for_provider_status(response, "voxcpm2")

        message = str(raised.exception)
        self.assertIn("provider-request-1", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("/home/private", message)
        self.assertNotIn("internal CUDA failure", message)


if __name__ == "__main__":
    unittest.main()
