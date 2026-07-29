from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from managed_backends.voxcpm2_compat_api import (
    LegacyTTSRequest,
    build_openai_payload,
    validate_sampling,
)


class VoxCPM2CompatibilityTests(unittest.TestCase):
    def test_plain_tts_maps_to_openai_speech_contract(self) -> None:
        payload = build_openai_payload(LegacyTTSRequest(text="Hola"))

        self.assertEqual(
            payload,
            {
                "input": "Hola",
                "voice": "default",
                "response_format": "wav",
            },
        )

    def test_clone_maps_reference_audio_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "speaker.wav"
            reference.write_bytes(b"RIFF")

            payload = build_openai_payload(
                LegacyTTSRequest(
                    text="Hola",
                    reference_wav_path=str(reference),
                    prompt_text="Texto original",
                ),
                root,
            )

        self.assertEqual(payload["ref_audio"], reference.resolve().as_uri())
        self.assertEqual(payload["ref_text"], "Texto original")

    def test_clone_rejects_reference_outside_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as allowed:
            with tempfile.NamedTemporaryFile(suffix=".wav") as reference:
                request = LegacyTTSRequest(
                    text="Hola",
                    reference_wav_path=reference.name,
                )
                with self.assertRaisesRegex(ValueError, "must be under"):
                    build_openai_payload(request, Path(allowed))

    def test_non_default_sampling_is_rejected(self) -> None:
        request = LegacyTTSRequest(
            text="Hola",
            cfg_value=1.5,
            inference_timesteps=10,
        )

        with self.assertRaisesRegex(ValueError, "cfg_value=2.0"):
            validate_sampling(request)


if __name__ == "__main__":
    unittest.main()
