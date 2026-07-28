from __future__ import annotations

import io
import unittest
import wave

from control_plane.tts.audio import normalize_to_wav


class TTSAudioTests(unittest.TestCase):
    def test_normalizes_wav_to_canonical_sample_rate_and_channels(self) -> None:
        source = io.BytesIO()
        with wave.open(source, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 2 * 1600)

        result, duration_ms = normalize_to_wav(
            source.getvalue(),
            "auto",
            48000,
            1,
        )

        with wave.open(io.BytesIO(result), "rb") as wav:
            self.assertEqual(wav.getframerate(), 48000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
        self.assertEqual(duration_ms, 100)


if __name__ == "__main__":
    unittest.main()

