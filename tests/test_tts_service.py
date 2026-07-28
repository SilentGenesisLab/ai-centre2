from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.config import Settings
from control_plane.tts.base import (
    PermanentTTSProviderError,
    RawSynthesisResult,
    TTSProvider,
    TransientTTSProviderError,
)
from control_plane.tts.schemas import (
    TTSSpeechRequest,
    TTSProviderName,
    VoiceProfile,
)
from control_plane.tts.service import TTSService


class FakeProvider(TTSProvider):
    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    async def synthesize(self, request, binding):
        self.calls += 1
        if self.error:
            raise self.error
        return RawSynthesisResult(
            audio=b"raw-audio",
            media_type="audio/mpeg",
            provider=self.name,
        )


class TTSServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            _env_file=None,
            service_token="test",
            control_runtime_dir=root,
            tts_voice_registry_path=root / "voices.json",
            tts_output_dir=root / "output",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_auto_route_falls_back_only_after_transient_failure(self) -> None:
        doubao = FakeProvider(
            "doubao",
            TransientTTSProviderError("temporary outage"),
        )
        elevenlabs = FakeProvider("elevenlabs")
        service = TTSService(self.settings, [doubao, elevenlabs])
        service.registry.put(
            VoiceProfile(
                voice_profile_id="voice-1",
                display_name="Voice 1",
                bindings={
                    "doubao": {"voice_type": "a"},
                    "elevenlabs": {"voice_id": "b"},
                },
                fallback_order=[
                    TTSProviderName.DOUBAO,
                    TTSProviderName.ELEVENLABS,
                ],
            )
        )
        request = TTSSpeechRequest(
            text="hola",
            language="es-MX",
            voice_profile_id="voice-1",
        )

        with patch(
            "control_plane.tts.service.normalize_to_wav",
            return_value=(b"RIFF", 700),
        ):
            result = await service.synthesize(request)

        self.assertEqual(result.provider, "elevenlabs")
        self.assertEqual(doubao.calls, 1)
        self.assertEqual(elevenlabs.calls, 1)

    async def test_permanent_failure_does_not_switch_voice_provider(self) -> None:
        doubao = FakeProvider(
            "doubao",
            PermanentTTSProviderError("invalid voice"),
        )
        elevenlabs = FakeProvider("elevenlabs")
        service = TTSService(self.settings, [doubao, elevenlabs])
        service.registry.put(
            VoiceProfile(
                voice_profile_id="voice-1",
                display_name="Voice 1",
                bindings={
                    "doubao": {"voice_type": "a"},
                    "elevenlabs": {"voice_id": "b"},
                },
                fallback_order=[
                    TTSProviderName.DOUBAO,
                    TTSProviderName.ELEVENLABS,
                ],
            )
        )

        with self.assertRaises(PermanentTTSProviderError):
            await service.synthesize(
                TTSSpeechRequest(
                    text="hola",
                    language="es-MX",
                    voice_profile_id="voice-1",
                )
            )

        self.assertEqual(elevenlabs.calls, 0)


if __name__ == "__main__":
    unittest.main()

