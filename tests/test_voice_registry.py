from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_plane.tts.schemas import TTSProviderName, VoiceProfile
from control_plane.tts.voices import VoiceRegistry


class VoiceRegistryTests(unittest.TestCase):
    def test_default_profile_is_created_and_updates_are_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = VoiceRegistry(Path(temp_dir) / "voices.json")

            default = registry.get("default")
            self.assertIn("voxcpm2", default.bindings)

            created = registry.put(
                VoiceProfile(
                    voice_profile_id="thai-female-1",
                    display_name="Thai female 1",
                    languages=["th-TH"],
                    bindings={
                        "doubao": {"voice_type": "voice-a"},
                        "elevenlabs": {"voice_id": "voice-b"},
                    },
                    fallback_order=[
                        TTSProviderName.DOUBAO,
                        TTSProviderName.ELEVENLABS,
                    ],
                )
            )
            updated = registry.put(created)

            self.assertEqual(updated.version, created.version + 1)
            self.assertEqual(
                registry.get("thai-female-1").fallback_order[0],
                TTSProviderName.DOUBAO,
            )


if __name__ == "__main__":
    unittest.main()

