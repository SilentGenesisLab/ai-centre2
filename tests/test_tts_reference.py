from __future__ import annotations

import io
import unittest
import wave

from control_plane.tts.reference import (
    append_wav_silence,
    candidate_window_starts,
    infer_text_language,
    languages_differ,
    reference_window_ranges,
    signal_to_noise_db,
    slice_wav,
    speech_coverage,
    wav_speech_segments,
    wav_duration_seconds,
    window_score,
)


def silence_wav(seconds: float, rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\0\0" * round(seconds * rate))
    return output.getvalue()


class TTSReferenceTests(unittest.TestCase):
    def test_language_routing_covers_representative_pairs(self) -> None:
        self.assertEqual(infer_text_language("欢迎使用", "auto"), "zh")
        self.assertEqual(infer_text_language("Hola mundo", "auto"), "es")
        self.assertEqual(infer_text_language("Hello world", "auto"), "en")
        self.assertEqual(infer_text_language("こんにちは", "auto"), "ja")
        self.assertTrue(languages_differ("spa", "zh-CN"))
        self.assertFalse(languages_differ("cmn", "zh-CN"))

    def test_reference_candidates_prefer_middle_five_seconds(self) -> None:
        starts = candidate_window_starts(10.0, [(0.2, 9.8)])
        self.assertEqual(starts[0], 2.5)
        self.assertIn(0.0, starts)
        self.assertIn(5.0, starts)

    def test_reference_candidates_snap_to_stable_100ms_grid(self) -> None:
        starts = candidate_window_starts(10.006, [(0.0, 9.98)])

        self.assertEqual(starts[0], 2.5)
        self.assertTrue(all(round(start, 1) == start for start in starts))

    def test_reference_slice_is_exactly_five_seconds(self) -> None:
        sliced = slice_wav(silence_wav(10), 2.5, 5.0)
        self.assertAlmostEqual(wav_duration_seconds(sliced), 5.0, places=3)

    def test_reference_tail_padding_is_finalized_silence(self) -> None:
        padded = append_wav_silence(silence_wav(4.0), 0.5)

        self.assertAlmostEqual(wav_duration_seconds(padded), 4.5, places=3)

    def test_reference_ranges_prefer_pause_bounded_speech(self) -> None:
        ranges = reference_window_ranges(
            12.0,
            [(0.5, 2.5), (2.8, 5.2), (5.6, 8.0), (8.4, 11.5)],
        )

        self.assertEqual(ranges[0], (2.8, 5.2))
        self.assertTrue(all(3.5 <= span <= 5.5 for _, span in ranges[:2]))

    def test_reference_ranges_keep_exact_five_second_alternative(self) -> None:
        ranges = reference_window_ranges(10.0, [(2.5, 8.0)])

        self.assertIn((2.5, 5.5), ranges)
        self.assertIn((2.5, 5.0), ranges)

    def test_energy_vad_and_snr_ignore_silent_edges(self) -> None:
        rate = 16_000
        frames = b"\0\0" * rate
        frames += b"\x10\x27" * rate * 4
        frames += b"\0\0" * rate
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(frames)

        segments = wav_speech_segments(output.getvalue())

        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0][0], 1.0, delta=0.05)
        self.assertAlmostEqual(segments[0][1], 5.0, delta=0.05)
        self.assertGreater(signal_to_noise_db(output.getvalue()), 25.0)

    def test_window_score_uses_speech_similarity_and_center_bias(self) -> None:
        center = window_score(
            speaker_similarity=0.64,
            coverage=speech_coverage(2.5, 7.5, [(0.2, 9.8)]),
            unclipped=1.0,
            start_seconds=2.5,
            duration_seconds=10.0,
        )
        edge = window_score(
            speaker_similarity=0.64,
            coverage=1.0,
            unclipped=1.0,
            start_seconds=0.0,
            duration_seconds=10.0,
        )
        self.assertGreater(center, edge)


if __name__ == "__main__":
    unittest.main()
