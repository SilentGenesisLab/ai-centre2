from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control_plane.config import Settings
from control_plane.tts.schemas import (
    TTSAsyncSpeechRequest,
    TTSEmotionStrategy,
)
from control_plane.tts.tasks import _synthesize_long_form


def tiny_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(48_000)
        target.writeframes(b"\x00\x00" * 480)
    return output.getvalue()


def internal_gap_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(48_000)
        target.writeframes(b"\x10\x27" * 9_600)
        target.writeframes(b"\x00\x00" * 48_000)
        target.writeframes(b"\x10\x27" * 9_600)
    return output.getvalue()


class FakeTask:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update_state(self, **kwargs) -> None:
        self.updates.append(kwargs)


class FakeService:
    async def synthesize(self, request):
        return SimpleNamespace(
            audio=tiny_wav(),
            provider="fake",
            provider_request_id=None,
        )


class RetryGapService:
    def __init__(self) -> None:
        self.seeds: list[int | None] = []

    async def synthesize(self, request):
        self.seeds.append(request.seed)
        return SimpleNamespace(
            audio=internal_gap_wav() if len(self.seeds) == 1 else tiny_wav(),
            provider="fake",
            provider_request_id=None,
        )


class LongFormTTSTests(unittest.TestCase):
    def test_internal_silence_retries_only_the_bad_segment_with_a_new_seed(self) -> None:
        request = TTSAsyncSpeechRequest(text="ทดสอบเสียงภาษาไทย", language="th")
        context = SimpleNamespace(
            style="",
            model_reference_path=None,
            model_prompt_text=None,
            effective_mode=None,
            fallback="none",
            reference_language=None,
            target_language="th",
            emotion_strategy=TTSEmotionStrategy.AUTO,
            enhancement_status="disabled",
        )
        service = RetryGapService()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                tts_output_dir=root / "output",
                tts_reference_dir=root / "reference",
            )
            output = settings.tts_output_dir / "result.wav"
            with (
                patch("control_plane.tts.tasks.get_settings", return_value=settings),
                patch("control_plane.tts.tasks.get_tts_service", return_value=service),
                patch(
                    "control_plane.api._prepare_clone_context",
                    new=AsyncMock(return_value=context),
                ),
            ):
                asyncio.run(
                    _synthesize_long_form(
                        FakeTask(),
                        request,
                        root / "work",
                        settings.tts_reference_dir / "job",
                        output,
                    )
                )

        self.assertEqual(service.seeds, [42, 43])

    def test_eight_thousand_characters_are_segmented_and_stream_combined(self) -> None:
        request = TTSAsyncSpeechRequest(text="长文本。" * 2000, language="zh")
        context = SimpleNamespace(
            style="",
            model_reference_path=None,
            model_prompt_text=None,
            effective_mode=None,
            fallback="none",
            reference_language=None,
            target_language="zh",
            emotion_strategy=TTSEmotionStrategy.AUTO,
            enhancement_status="disabled",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                tts_output_dir=root / "output",
                tts_reference_dir=root / "reference",
            )
            output = settings.tts_output_dir / "result.wav"
            task = FakeTask()
            with (
                patch("control_plane.tts.tasks.get_settings", return_value=settings),
                patch("control_plane.tts.tasks.get_tts_service", return_value=FakeService()),
                patch(
                    "control_plane.api._prepare_clone_context",
                    new=AsyncMock(return_value=context),
                ),
            ):
                result = asyncio.run(
                    _synthesize_long_form(
                        task,
                        request,
                        root / "work",
                        settings.tts_reference_dir / "job",
                        output,
                    )
                )

            self.assertTrue(output.is_file())
            self.assertGreater(result["segment_count"], 1)
            self.assertEqual(result["provider"], "fake")
            self.assertEqual(task.updates[-1]["meta"]["stage"], "combining")


if __name__ == "__main__":
    unittest.main()
