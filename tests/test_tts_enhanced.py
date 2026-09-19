from __future__ import annotations

import io
import math
import unittest
import unicodedata
import wave
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from control_plane.config import Settings
from control_plane.tts.enhanced import (
    TextSegment,
    apply_prosody_wav,
    build_style_instruction,
    combine_wav_segments,
    combine_wav_segment_files,
    enhance_emotion,
    model_text,
    normalize_wav_silence,
    sanitize_tts_model_text,
    split_tts_text,
    trim_wav_trailing_silence,
    wav_duration_seconds,
    wav_silence_metrics,
)
from control_plane.tts.schemas import ProsodySpec


def sine_wav(
    seconds: float = 1.0,
    frequency: float = 440.0,
    rate: int = 48_000,
) -> bytes:
    frames = bytearray()
    for index in range(round(rate * seconds)):
        sample = round(math.sin(index / rate * frequency * math.tau) * 8_000)
        frames.extend(sample.to_bytes(2, "little", signed=True))
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(frames)
    return output.getvalue()


def wav_samples(audio: bytes) -> tuple[list[int], int]:
    with wave.open(io.BytesIO(audio), "rb") as source:
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    return [
        int.from_bytes(frames[index:index + 2], "little", signed=True)
        for index in range(0, len(frames), 2)
    ], rate


def rms(samples: list[int]) -> float:
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def positive_zero_crossing_frequency(samples: list[int], rate: int) -> float:
    crossings = sum(
        previous <= 0 < current
        for previous, current in zip(samples, samples[1:])
    )
    return crossings / (len(samples) / rate)


class EnhancedTTSTests(unittest.TestCase):
    def test_thai_long_text_uses_short_segments(self) -> None:
        text = "ข้อความภาษาไทยสำหรับทดสอบการแบ่งเสียงยาว" * 20

        segments = split_tts_text(text)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertTrue(all(len(segment.text) <= 120 for segment in segments))

    def test_thai_split_keeps_combining_marks_with_their_base(self) -> None:
        text = "ก้" * 20

        segments = split_tts_text(text, limit=5)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertTrue(
            all(
                not unicodedata.category(segment.text[0]).startswith("M")
                for segment in segments
            )
        )

    def test_thai_split_prefers_phrase_spaces(self) -> None:
        text = (
            "ข้อความภาษาไทยสำหรับทดสอบ "
            "การแบ่งเสียงยาวที่ชัดเจน "
            "และตรวจสอบอีกครั้ง"
        )

        segments = split_tts_text(text, limit=32)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertTrue(all(segment.text.endswith(" ") for segment in segments[:-1]))

    def test_latin_languages_do_not_split_words_when_spaces_are_available(self) -> None:
        samples = (
            "Alpha bravo charlie delta echo foxtrot.",
            "La información correcta mejora muchísimo la narración.",
            "A informação correta melhora muito a narração.",
        )

        for text in samples:
            with self.subTest(text=text):
                segments = split_tts_text(text, limit=16)
                self.assertEqual("".join(segment.text for segment in segments), text)
                self.assertTrue(
                    all(segment.text[-1].isspace() for segment in segments[:-1])
                )

    def test_latin_sentence_boundaries_avoid_abbreviations_urls_and_decimals(self) -> None:
        text = (
            "Dr. Silva uses https://example.com and value 123.45. "
            "Next sentence continues clearly."
        )

        segments = split_tts_text(text, limit=58)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].text.endswith("123.45."))
        self.assertNotIn("https://example.\n", "\n".join(s.text for s in segments))

    def test_multilingual_split_is_unicode_safe_and_lossless(self) -> None:
        text = "中文段落 cafe\u0301 ภาษาไทย 👩‍💻 português español 中文段落"

        segments = split_tts_text(text, limit=13)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertTrue(all(len(segment.text) <= 13 for segment in segments))
        self.assertTrue(
            all(
                not unicodedata.category(segment.text[0]).startswith("M")
                and segment.text[0] != "\u200d"
                for segment in segments
            )
        )
        self.assertTrue(all(not segment.text.endswith("\u200d") for segment in segments))

    def test_chinese_split_prefers_punctuation_and_preserves_all_text(self) -> None:
        text = "这是第一句话，这是补充说明。这是第二句话，这是更多说明。"

        segments = split_tts_text(text, limit=12)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertTrue(
            all(segment.text[-1] in "，。" for segment in segments[:-1])
        )

    def test_split_avoids_a_tiny_final_fragment(self) -> None:
        text = "word " * 23 + "ok"

        segments = split_tts_text(text, limit=100)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertTrue(all(len(segment.text) <= 100 for segment in segments))
        self.assertGreaterEqual(len(segments[-1].text), 100 // 3)

    def test_detects_and_trims_generated_digital_zero_tail(self) -> None:
        samples, rate = wav_samples(sine_wav(seconds=0.5))
        samples.extend([0] * round(rate * 2.0))
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(
                b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
            )

        metrics = wav_silence_metrics(output.getvalue())
        trimmed = trim_wav_trailing_silence(output.getvalue())

        self.assertGreaterEqual(metrics["trailing_silence_seconds"], 1.95)
        self.assertTrue(metrics["silent_tail_anomaly"])
        self.assertAlmostEqual(wav_duration_seconds(trimmed), 0.70, delta=0.04)

    def test_detects_internal_generated_silence(self) -> None:
        first, rate = wav_samples(sine_wav(seconds=0.4))
        last, _ = wav_samples(sine_wav(seconds=0.4, frequency=550.0))
        samples = first + [0] * round(rate * 1.2) + last
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(
                b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
            )

        metrics = wav_silence_metrics(output.getvalue())

        self.assertGreaterEqual(metrics["max_internal_silence_seconds"], 1.15)
        self.assertTrue(metrics["internal_silence_anomaly"])

    def test_collapses_internal_generated_silence_without_cutting_sound(self) -> None:
        first, rate = wav_samples(sine_wav(seconds=0.4))
        last, _ = wav_samples(sine_wav(seconds=0.4, frequency=550.0))
        samples = first + [0] * round(rate * 1.2) + last
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(
                b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
            )

        normalized = normalize_wav_silence(output.getvalue())

        self.assertFalse(wav_silence_metrics(normalized)["internal_silence_anomaly"])
        self.assertAlmostEqual(wav_duration_seconds(normalized), 1.05, delta=0.04)

    def test_does_not_turn_an_entirely_silent_bad_segment_into_a_valid_blip(self) -> None:
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(48_000)
            target.writeframes(b"\x00\x00" * 48_000)
        audio = output.getvalue()

        self.assertEqual(trim_wav_trailing_silence(audio), audio)

    def test_combines_segment_files_without_full_result_buffer(self) -> None:
        segments = [
            TextSegment(text="first", boundary="sentence"),
            TextSegment(text="second", boundary="paragraph"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "1.wav", root / "2.wav"]
            paths[0].write_bytes(sine_wav(seconds=0.1, rate=32_000))
            paths[1].write_bytes(sine_wav(seconds=0.1, rate=32_000))
            target = root / "combined.wav"

            metadata = combine_wav_segment_files(paths, segments, target)

            self.assertEqual(metadata["sample_rate"], 32_000)
            self.assertEqual(metadata["channels"], 1)
            self.assertAlmostEqual(
                wav_duration_seconds(target.read_bytes()),
                0.32,
                delta=0.01,
            )

    def test_combines_segments_at_requested_sample_rate(self) -> None:
        segments = [
            TextSegment(text="first", boundary="sentence"),
            TextSegment(text="second", boundary="sentence"),
        ]

        result = combine_wav_segments(
            [sine_wav(seconds=0.1, rate=32_000)] * 2,
            segments,
        )

        _, rate = wav_samples(result)
        self.assertEqual(rate, 32_000)
        self.assertAlmostEqual(wav_duration_seconds(result), 0.32, delta=0.01)

    def test_rejects_segments_with_different_sample_rates(self) -> None:
        segments = [
            TextSegment(text="first", boundary="sentence"),
            TextSegment(text="second", boundary="sentence"),
        ]

        with self.assertRaisesRegex(ValueError, "segment WAV format mismatch"):
            combine_wav_segments(
                [sine_wav(rate=32_000), sine_wav(rate=48_000)],
                segments,
            )

    def test_model_text_removes_non_spoken_continuation_markers(self) -> None:
        self.assertEqual(
            sanitize_tts_model_text("* (Hello) - world `test` AI-2"),
            "Hello world test AI-2",
        )

    def test_chinese_long_text_is_split_without_loss_or_reordering(self) -> None:
        text = "第一段内容。" * 30 + "\n" + "第二段没有句号" * 30
        segments = split_tts_text(text)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertGreater(len(segments), 2)
        self.assertTrue(all(len(segment.text) <= 120 for segment in segments))

    def test_cross_language_clone_can_split_chinese_at_short_clause_limit(self) -> None:
        text = "今天的天气很温暖，我们准备了一段自然流畅的语音，希望每一个字都清楚准确。"

        segments = split_tts_text(text, limit=20)

        self.assertEqual("".join(segment.text for segment in segments), text)
        self.assertEqual(len(segments), 3)
        self.assertTrue(all(len(segment.text) <= 20 for segment in segments))

    def test_clone_never_prefixes_style_to_business_text(self) -> None:
        style = build_style_instruction(
            "真诚、温暖（不要朗读）",
            ProsodySpec(speed=1.2, volume=1.0, pitch=1.0),
            cloning=True,
        )
        plain_result = model_text("欢迎使用。", style)
        clone_result = model_text("欢迎使用。", style, cloning=True)

        self.assertTrue(plain_result.endswith("欢迎使用。"))
        self.assertTrue(plain_result.startswith("("))
        self.assertEqual(clone_result, "欢迎使用。")
        self.assertNotIn("保持参考音频", style)
        self.assertNotIn("（", plain_result)

    def test_speed_volume_and_pitch_are_applied_by_ffmpeg(self) -> None:
        source = sine_wav()
        result = apply_prosody_wav(
            source,
            "auto",
            ProsodySpec(speed=2.0, volume=0.5, pitch=1.1),
        )

        self.assertAlmostEqual(wav_duration_seconds(result), 0.5, delta=0.08)

    def test_volume_changes_rms_without_clipping(self) -> None:
        source = sine_wav(seconds=2.0)
        result = apply_prosody_wav(
            source,
            "auto",
            ProsodySpec(speed=1.0, volume=0.5, pitch=1.0),
        )
        source_samples, _ = wav_samples(source)
        result_samples, _ = wav_samples(result)

        self.assertAlmostEqual(rms(result_samples) / rms(source_samples), 0.5, delta=0.08)
        self.assertLessEqual(max(abs(sample) for sample in result_samples), 32767)

    def test_pitch_changes_frequency_without_changing_duration(self) -> None:
        source = sine_wav(seconds=2.0)
        result = apply_prosody_wav(
            source,
            "auto",
            ProsodySpec(speed=1.0, volume=1.0, pitch=1.5),
        )
        samples, rate = wav_samples(result)

        self.assertAlmostEqual(wav_duration_seconds(result), 2.0, delta=0.08)
        self.assertAlmostEqual(
            positive_zero_crossing_frequency(samples, rate),
            660.0,
            delta=25.0,
        )


class _EmotionResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {"message": {"content": '{"style_instruction":"温暖、自然停顿"}'}}
            ]
        }


class _EmotionClient:
    payload: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, _url: str, *, headers: dict, json: dict):
        self.__class__.payload = json
        return _EmotionResponse()


class _TimeoutEmotionClient(_EmotionClient):
    async def post(self, _url: str, *, headers: dict, json: dict):
        raise httpx.ReadTimeout("timeout")


class EmotionEnhancementTests(unittest.IsolatedAsyncioTestCase):
    def settings(self) -> Settings:
        return Settings(
            service_token="test-token",
            doubao_emotion_api_key="test-key",
            doubao_emotion_model="doubao-seed-2-1-turbo-260628",
        )

    async def test_disables_thinking_and_limits_structured_output(self) -> None:
        with patch(
            "control_plane.tts.enhanced.httpx.AsyncClient",
            _EmotionClient,
        ):
            style, status = await enhance_emotion(
                self.settings(),
                "欢迎了解我们的新产品。",
                "温暖",
                True,
            )

        self.assertEqual(status, "enhanced")
        self.assertEqual(style, "温暖、自然停顿")
        self.assertEqual(_EmotionClient.payload["thinking"], {"type": "disabled"})
        self.assertEqual(_EmotionClient.payload["max_tokens"], 180)

    async def test_timeout_falls_back_to_original_emotion(self) -> None:
        with patch(
            "control_plane.tts.enhanced.httpx.AsyncClient",
            _TimeoutEmotionClient,
        ):
            style, status = await enhance_emotion(
                self.settings(),
                "欢迎了解我们的新产品。",
                "温暖",
                True,
            )

        self.assertEqual((style, status), ("温暖", "fallback"))


if __name__ == "__main__":
    unittest.main()
