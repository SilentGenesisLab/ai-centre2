from __future__ import annotations

import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from control_plane.api import (
    ASRUrlRequest,
    CloneContext,
    FaceUrlJobRequest,
    OCRUrlBatchRequest,
    OCRUrlImage,
    TTSPublicSpeechRequest,
    _REFERENCE_EMOTION_CACHE,
    _REFERENCE_WINDOW_CACHE,
    _SPEAKER_SIMILARITY_CACHE,
    _content_quality,
    _prepare_clone_context,
    _reference_emotion,
    _quality_metrics,
    _quality_asr_language,
    _resolve_prompt_text,
    _select_reference_window,
    _speaker_similarity,
    _strict_enhanced_response,
    create_face_url_job,
    create_face_url_job_and_wait,
    ocr_url_batch,
    synthesize_v2,
    transcribe,
)
from control_plane.config import Settings
from control_plane.media_fetch import DownloadedMedia
from control_plane.tts.base import SynthesisResult
from control_plane.tts.base import TransientTTSProviderError
from control_plane.tts.schemas import TTSCloneSpeechRequest
from control_plane.tts.schemas import TTSCloneMode, TTSEmotionStrategy


class FakeTTSService:
    def __init__(self) -> None:
        self.request = None
        self.calls = 0

    async def synthesize(self, request):
        self.calls += 1
        self.request = request
        return SynthesisResult(b"RIFFaudio", "voxcpm2", 900)


class FakeHttpClient:
    last_json = None

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, json):
        FakeHttpClient.last_json = json
        request = httpx.Request("POST", "http://ocr/v1/ocr/batch")
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json", "x-ocr-worker": "gpu0"},
            json={"job_id": json["job_id"], "results": []},
        )


class FakeEmotionResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"label": "happy"}


class FakeEmotionClient:
    calls = 0

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, files):
        self.__class__.calls += 1
        return FakeEmotionResponse()


class FakeSpeakerResponse(FakeEmotionResponse):
    def json(self) -> dict[str, float]:
        return {"similarity": 0.75}


class FakeSpeakerClient(FakeEmotionClient):
    async def post(self, _url, files):
        self.__class__.calls += 1
        return FakeSpeakerResponse()


def tone_wav(seconds: float, rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\x10\x27" * round(seconds * rate))
    return output.getvalue()


def quality_metrics(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "quality_passed": True,
        "content_passed": True,
        "content_exact": True,
        "content_edits": 0,
        "content_allowed_edits": 1,
        "content_cer": 0.0,
        "content_phonetic_edits": 0,
        "content_phonetic_cer": 0.0,
        "speaker_similarity": 0.7,
        "speaker_passed": True,
        "speed_ratio": 1.0,
        "speed_passed": True,
        "emotion_score": None,
        "emotion_passed": True,
        "first_sound_duration_seconds": 0.1,
        "last_sound_duration_seconds": 0.1,
        "duration_seconds": 1.0,
        "first_sound_anomaly": False,
        "last_sound_anomaly": False,
        "longest_sound_anomaly": False,
        "duration_anomaly": False,
        "energetic_tail": False,
        "unexpected_prefix": False,
        "unexpected_suffix": False,
        "quality_score": 0.9,
    }
    values.update(overrides)
    return values


class UrlMediaApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            _env_file=None,
            service_token="test",
            control_runtime_dir=root / "control",
            remote_input_dir=root / "remote-inputs",
            tts_reference_dir=root / "references",
            tts_voice_registry_path=root / "voices.json",
            tts_output_dir=root / "tts-output",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_asr_url_download_is_removed_after_transcription(self) -> None:
        async def download(_url, directory, stem, *_args, **_kwargs):
            directory.mkdir(parents=True)
            path = directory / f"{stem}.wav"
            path.write_bytes(b"audio")
            return DownloadedMedia(path, 5, "audio/wav")

        transcribe_content = AsyncMock(return_value={"text": "ok"})
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.download_public_media_async", side_effect=download),
            patch("control_plane.api._transcribe_content", transcribe_content),
        ):
            result = await transcribe(
                ASRUrlRequest(file_url="https://cdn.example/audio.wav")
            )

        self.assertEqual(result, {"text": "ok"})
        self.assertEqual(list(self.settings.remote_input_dir.glob("asr-*")), [])

    async def test_cross_language_clone_never_forwards_reference_transcript(self) -> None:
        directory = self.settings.tts_reference_dir / "cross-language"
        directory.mkdir(parents=True)
        reference = directory / "reference.mp3"
        reference.write_bytes(b"audio")
        request = TTSPublicSpeechRequest(
            text="欢迎体验我们的新产品。",
            language="auto",
            reference_audio_url="https://cdn.example/reference.mp3",
            prompt_text="",
            emotion="真诚、温暖、有感染力",
            emotion_enhance=True,
            clone_mode="ultimate",
        )
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._reference_transcription",
                AsyncMock(return_value=("Hola mundo", "es", {})),
            ),
            patch(
                "control_plane.api._select_reference_window",
                AsyncMock(return_value=reference),
            ),
            patch(
                "control_plane.api._reference_emotion",
                AsyncMock(return_value="happy"),
            ),
        ):
            context = await _prepare_clone_context(request, reference, directory)

        self.assertTrue(context.cross_language)
        self.assertEqual(context.effective_mode, TTSCloneMode.CONTROLLABLE)
        self.assertIsNone(context.model_prompt_text)
        self.assertEqual(context.audit_prompt_text, "Hola mundo")
        self.assertEqual(context.fallback, "cross-language-ultimate-disabled")
        self.assertEqual(context.emotion_strategy, TTSEmotionStrategy.INHERIT)
        self.assertEqual(context.style, "")
        self.assertEqual(context.enhancement_status, "inherited")

    async def test_same_language_ultimate_keeps_full_reference_aligned_with_prompt(self) -> None:
        directory = self.settings.tts_reference_dir / "same-language"
        directory.mkdir(parents=True)
        reference = directory / "reference.wav"
        selected = directory / "reference-selected.wav"
        reference.write_bytes(tone_wav(8.0))
        selected.write_bytes(tone_wav(5.5))
        request = TTSPublicSpeechRequest(
            text="Welcome back.",
            language="en",
            reference_audio_url="https://cdn.example/reference.wav",
            prompt_text="This is the complete reference text.",
        )
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._reference_transcription",
                AsyncMock(
                    return_value=(
                        "This is the complete reference text.",
                        "en",
                        {},
                    )
                ),
            ),
            patch(
                "control_plane.api._select_reference_window",
                AsyncMock(return_value=selected),
            ),
        ):
            context = await _prepare_clone_context(request, reference, directory)

        self.assertEqual(context.effective_mode, TTSCloneMode.ULTIMATE)
        self.assertEqual(context.model_reference_path, reference)
        self.assertEqual(context.isolated_reference_path, selected)
        self.assertEqual(
            context.model_prompt_text,
            "This is the complete reference text.",
        )
        self.assertIsNone(context.reference_window_start)
        self.assertIsNone(context.reference_window_duration)

    async def test_reference_emotion_is_cached_by_audio_hash(self) -> None:
        reference = Path(self.temporary.name) / "same-reference.wav"
        reference.write_bytes(b"same-reference-audio")
        _REFERENCE_EMOTION_CACHE.clear()
        FakeEmotionClient.calls = 0
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.httpx.AsyncClient", FakeEmotionClient),
        ):
            first = await _reference_emotion(reference)
            second = await _reference_emotion(reference)

        self.assertEqual((first, second), ("happy", "happy"))
        self.assertEqual(FakeEmotionClient.calls, 1)

    async def test_reference_window_cache_skips_repeated_speaker_scoring(self) -> None:
        reference = Path(self.temporary.name) / "reference.wav"
        reference.write_bytes(tone_wav(10.0))
        first_directory = Path(self.temporary.name) / "first-window"
        second_directory = Path(self.temporary.name) / "second-window"
        first_directory.mkdir()
        second_directory.mkdir()
        _REFERENCE_WINDOW_CACHE.clear()
        similarity = AsyncMock(return_value=0.8)
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api._speaker_similarity", similarity),
        ):
            first = await _select_reference_window(reference, {}, first_directory)
            first_call_count = similarity.await_count
            second = await _select_reference_window(reference, {}, second_directory)

        self.assertGreater(first_call_count, 0)
        self.assertEqual(similarity.await_count, first_call_count)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    async def test_speaker_similarity_is_cached_by_both_audio_hashes(self) -> None:
        reference = Path(self.temporary.name) / "speaker.wav"
        reference.write_bytes(b"reference-speaker")
        _SPEAKER_SIMILARITY_CACHE.clear()
        FakeSpeakerClient.calls = 0
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.httpx.AsyncClient", FakeSpeakerClient),
        ):
            first = await _speaker_similarity(reference, b"same-candidate")
            second = await _speaker_similarity(reference, b"same-candidate")

        self.assertEqual((first, second), (0.75, 0.75))
        self.assertEqual(FakeSpeakerClient.calls, 1)

    async def test_tts_reference_url_forces_clone_and_is_removed(self) -> None:
        async def download(_url, directory, stem, *_args, **_kwargs):
            directory.mkdir(parents=True)
            path = directory / f"{stem}.wav"
            path.write_bytes(b"audio")
            return DownloadedMedia(path, 5, "audio/wav")

        service = FakeTTSService()
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.download_public_media_async", side_effect=download),
            patch("control_plane.api.get_tts_service", return_value=service),
            patch("control_plane.api._run_background", side_effect=lambda coroutine: coroutine.close()),
            patch(
                "control_plane.api._speaker_similarity",
                AsyncMock(return_value=0.5),
            ),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(
                    side_effect=[
                        {"text": "reference words"},
                        {"text": "hello"},
                    ]
                ),
            ) as transcribe_reference,
        ):
            response = await synthesize_v2(
                TTSPublicSpeechRequest(
                    text="hello",
                    provider="elevenlabs",
                    reference_audio_url="https://cdn.example/reference.wav",
                )
            )

        self.assertIsInstance(service.request, TTSCloneSpeechRequest)
        self.assertEqual(service.request.provider.value, "voxcpm2")
        self.assertEqual(service.request.language, "auto")
        self.assertIsNone(service.request.prompt_text)
        self.assertEqual(transcribe_reference.await_count, 2)
        self.assertEqual(response.headers["x-tts-provider"], "voxcpm2")
        self.assertEqual(response.headers["x-tts-quality-status"], "completed")
        self.assertEqual(response.headers["x-tts-quality-attempts"], "1")
        self.assertEqual(response.headers["x-tts-clone-mode"], "controllable")

    async def test_nonempty_tts_prompt_skips_asr(self) -> None:
        async def download(_url, directory, stem, *_args, **_kwargs):
            directory.mkdir(parents=True)
            path = directory / f"{stem}.wav"
            path.write_bytes(b"audio")
            return DownloadedMedia(path, 5, "audio/wav")

        service = FakeTTSService()
        transcribe_reference = AsyncMock(return_value={"text": "hello"})
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.download_public_media_async", side_effect=download),
            patch("control_plane.api.get_tts_service", return_value=service),
            patch("control_plane.api._run_background", side_effect=lambda coroutine: coroutine.close()),
            patch(
                "control_plane.api._speaker_similarity",
                AsyncMock(return_value=0.5),
            ),
            patch(
                "control_plane.api._transcribe_content",
                transcribe_reference,
            ),
        ):
            await synthesize_v2(
                TTSPublicSpeechRequest(
                    text="hello",
                    reference_audio_url="https://cdn.example/reference.wav",
                    prompt_text="  exact reference words  ",
                )
            )

        self.assertEqual(service.request.prompt_text, "exact reference words")
        transcribe_reference.assert_awaited_once()

    async def test_empty_tts_prompt_joins_asr_segments(self) -> None:
        reference = Path(self.temporary.name) / "reference.wav"
        reference.write_bytes(b"audio")
        with patch(
            "control_plane.api._transcribe_content",
            AsyncMock(
                return_value={
                    "segments": [
                        {"text": "第一段"},
                        {"text": "第二段"},
                    ]
                }
            ),
        ):
            prompt = await _resolve_prompt_text(reference, "   ")

        self.assertEqual(prompt, "第一段 第二段")

    async def test_standard_clone_retries_only_after_failed_quality_gate(self) -> None:
        async def download(_url, directory, stem, *_args, **_kwargs):
            directory.mkdir(parents=True)
            path = directory / f"{stem}.wav"
            path.write_bytes(b"audio")
            return DownloadedMedia(path, 5, "audio/wav")

        service = FakeTTSService()
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.download_public_media_async", side_effect=download),
            patch("control_plane.api.get_tts_service", return_value=service),
            patch(
                "control_plane.api._speaker_similarity",
                AsyncMock(return_value=0.5),
            ),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(side_effect=[{"text": "wrong"}, {"text": "hello"}]),
            ),
        ):
            response = await synthesize_v2(
                TTSPublicSpeechRequest(
                    text="hello",
                    reference_audio_url="https://cdn.example/reference.wav",
                    prompt_text="exact reference words",
                    quality_mode="standard",
                )
            )

        self.assertEqual(service.calls, 2)
        self.assertEqual(response.headers["x-tts-quality-attempts"], "2")

    async def test_clone_returns_best_candidate_after_three_failed_gates(self) -> None:
        async def download(_url, directory, stem, *_args, **_kwargs):
            directory.mkdir(parents=True)
            path = directory / f"{stem}.wav"
            path.write_bytes(b"audio")
            return DownloadedMedia(path, 5, "audio/wav")

        service = FakeTTSService()
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.download_public_media_async", side_effect=download),
            patch("control_plane.api.get_tts_service", return_value=service),
            patch(
                "control_plane.api._speaker_similarity",
                AsyncMock(side_effect=[0.1, 0.6, 0.3, 0.7]),
            ),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value={"text": "wrong"}),
            ),
        ):
            response = await synthesize_v2(
                TTSPublicSpeechRequest(
                    text="hello",
                    reference_audio_url="https://cdn.example/reference.wav",
                    prompt_text="exact reference words",
                    quality_mode="strict",
                )
            )

        self.assertEqual(service.calls, 4)
        self.assertEqual(response.headers["x-tts-selected-attempt"], "4")
        self.assertEqual(response.headers["x-tts-quality-passed"], "false")
        self.assertIn("content", response.headers["x-tts-degraded-reason"])
        self.assertEqual(
            response.headers["x-tts-clone-fallback"],
            "ultimate-quality-failed-controllable",
        )

    async def test_standard_short_tts_returns_after_first_passing_candidate(self) -> None:
        synthesize = AsyncMock(return_value=(tone_wav(1.0), "voxcpm2", 1))
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(return_value=quality_metrics()),
            ),
        ):
            response = await synthesize_v2(TTSPublicSpeechRequest(text="hello"))

        self.assertEqual(synthesize.await_count, 1)
        self.assertEqual(response.headers["x-tts-quality-attempts"], "1")
        self.assertEqual(response.headers["x-tts-quality-passed"], "true")

    async def test_short_tts_retries_after_backend_produces_no_audio(self) -> None:
        synthesize = AsyncMock(
            side_effect=[
                TransientTTSProviderError("no audio"),
                (tone_wav(1.0), "voxcpm2", 1),
            ]
        )
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(return_value=quality_metrics()),
            ),
        ):
            response = await synthesize_v2(TTSPublicSpeechRequest(text="好"))

        self.assertEqual(synthesize.await_count, 2)
        self.assertEqual(synthesize.await_args_list[0].kwargs["seed"], 45)
        self.assertEqual(synthesize.await_args_list[1].kwargs["seed"], 46)
        self.assertEqual(response.headers["x-tts-quality-passed"], "true")
        self.assertEqual(response.headers["x-tts-quality-attempts"], "2")
        self.assertEqual(response.headers["x-tts-selected-attempt"], "2")

    async def test_short_japanese_starts_from_verified_seed_45(self) -> None:
        synthesize = AsyncMock(return_value=(tone_wav(1.0), "voxcpm2", 1))
        with (
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(return_value=quality_metrics()),
            ),
        ):
            response = await _strict_enhanced_response(
                TTSPublicSpeechRequest(text="お帰りなさい。", language="ja"),
                CloneContext(target_language="ja"),
                "request-id",
            )

        self.assertEqual(synthesize.await_count, 1)
        self.assertEqual(synthesize.await_args.kwargs["seed"], 45)
        self.assertEqual(response.headers["x-tts-quality-passed"], "true")

    async def test_cross_language_candidates_never_add_a_synthetic_primer(self) -> None:
        request = TTSPublicSpeechRequest(
            text="欢迎体验我们的新产品。",
            reference_audio_url="https://cdn.example/reference.wav",
        )
        context = CloneContext(
            model_reference_path=Path("reference.wav"),
            audit_prompt_text="Hola mundo",
            reference_language="es",
            target_language="zh",
            effective_mode=TTSCloneMode.CONTROLLABLE,
            cross_language=True,
            emotion_strategy=TTSEmotionStrategy.INHERIT,
        )
        metrics = {
            "quality_passed": False,
            "content_edits": 0,
            "content_allowed_edits": 0,
            "content_cer": 0.0,
            "content_phonetic_edits": 0,
            "content_phonetic_cer": 0.0,
            "speaker_similarity": 0.5,
            "speed_ratio": 1.0,
            "emotion_score": None,
            "first_sound_duration_seconds": 0.12,
            "first_sound_anomaly": False,
            "unexpected_prefix": False,
            "quality_score": 0.9,
        }
        synthesize = AsyncMock(return_value=(b"audio", "voxcpm2", 1))
        with (
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(return_value=metrics),
            ),
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                "request-id",
                always_three=True,
            )

        self.assertEqual(synthesize.await_count, 3)
        self.assertEqual(response.headers["x-tts-quality-attempts"], "3")
        self.assertEqual(response.headers["x-tts-reference-fallback"], "none")

    async def test_cross_language_ranking_prefers_similarity_when_edges_are_clean(self) -> None:
        request = TTSPublicSpeechRequest(
            text="欢迎体验我们的新产品。",
            reference_audio_url="https://cdn.example/reference.wav",
        )
        context = CloneContext(
            model_reference_path=Path("reference.wav"),
            cross_language=True,
            effective_mode=TTSCloneMode.CONTROLLABLE,
        )

        def metrics(
            onset: float,
            similarity: float,
            duration: float,
        ) -> dict[str, object]:
            return {
                "quality_passed": False,
                "content_edits": 0,
                "content_allowed_edits": 0,
                "content_cer": 0.0,
                "content_phonetic_edits": 0,
                "content_phonetic_cer": 0.0,
                "speaker_similarity": similarity,
                "speed_ratio": 1.0,
                "emotion_score": None,
                "first_sound_duration_seconds": onset,
                "first_sound_anomaly": False,
                "unexpected_prefix": False,
                "duration_seconds": duration,
                "quality_score": 0.8,
            }

        with (
            patch(
                "control_plane.api._synthesize_enhanced_once",
                AsyncMock(return_value=(b"audio", "voxcpm2", 1)),
            ),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(
                    side_effect=[
                        metrics(0.12, 0.40, 1.0),
                        metrics(0.34, 0.75, 1.4),
                        metrics(0.20, 0.60, 1.1),
                    ]
                ),
            ),
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                "request-id",
                always_three=True,
            )

        self.assertEqual(response.headers["x-tts-selected-attempt"], "2")
        self.assertEqual(response.headers["x-tts-first-sound-duration"], "0.3400")

    async def test_cross_language_adds_candidates_only_after_first_three_fail(self) -> None:
        request = TTSPublicSpeechRequest(
            text="欢迎回来。",
            reference_audio_url="https://cdn.example/reference.wav",
        )
        context = CloneContext(
            model_reference_path=Path("reference.wav"),
            cross_language=True,
            effective_mode=TTSCloneMode.CONTROLLABLE,
        )
        failed = quality_metrics(
            quality_passed=False,
            speaker_passed=False,
            speaker_similarity=0.45,
        )
        passed = quality_metrics(speaker_similarity=0.72)
        synthesize = AsyncMock(return_value=(b"audio", "voxcpm2", 1))
        with (
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(side_effect=[failed, failed, failed, failed, failed, passed]),
            ),
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                "request-id",
            )

        self.assertEqual(synthesize.await_count, 6)
        self.assertEqual(
            [call.kwargs["seed"] for call in synthesize.await_args_list],
            [43, 44, 45, 46, 52, 53],
        )
        self.assertEqual(response.headers["x-tts-quality-attempts"], "6")
        self.assertEqual(response.headers["x-tts-selected-attempt"], "6")
        self.assertEqual(response.headers["x-tts-quality-passed"], "true")

    async def test_failed_ultimate_candidates_fall_back_to_isolated_reference(self) -> None:
        request = TTSPublicSpeechRequest(
            text="Welcome back.",
            language="en",
            reference_audio_url="https://cdn.example/reference.wav",
        )
        original = Path("reference-full.wav")
        isolated = Path("reference-selected.wav")
        context = CloneContext(
            original_reference_path=original,
            model_reference_path=original,
            isolated_reference_path=isolated,
            audit_prompt_text="Complete reference text.",
            model_prompt_text="Complete reference text.",
            reference_language="en",
            target_language="en",
            effective_mode=TTSCloneMode.ULTIMATE,
        )
        failed = quality_metrics(
            quality_passed=False,
            content_passed=False,
            content_exact=False,
            content_edits=3,
            content_cer=0.3,
            content_phonetic_edits=3,
            content_phonetic_cer=0.3,
            unexpected_prefix=True,
            quality_score=0.2,
        )
        synthesize = AsyncMock(
            side_effect=[
                (b"ultimate-1", "voxcpm2", 1),
                (b"ultimate-2", "voxcpm2", 1),
                (b"ultimate-3", "voxcpm2", 1),
                (b"controllable", "voxcpm2", 1),
            ]
        )
        with (
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(side_effect=[failed, failed, failed, quality_metrics()]),
            ),
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                "request-id",
            )

        self.assertEqual(synthesize.await_count, 4)
        self.assertEqual(synthesize.await_args_list[-1].args[1], isolated)
        self.assertIsNone(synthesize.await_args_list[-1].args[2])
        self.assertEqual(response.body, b"controllable")
        self.assertEqual(response.headers["x-tts-quality-attempts"], "4")
        self.assertEqual(response.headers["x-tts-selected-attempt"], "4")
        self.assertEqual(response.headers["x-tts-quality-passed"], "true")
        self.assertEqual(response.headers["x-tts-clone-mode"], "controllable")
        self.assertEqual(
            response.headers["x-tts-clone-fallback"],
            "ultimate-quality-failed-controllable",
        )

    async def test_explicit_speed_prefers_an_observable_candidate(self) -> None:
        request = TTSPublicSpeechRequest(
            text="Welcome back.",
            language="en",
            reference_audio_url="https://cdn.example/reference.wav",
            prosody={"speed": 1.2, "volume": 1.0, "pitch": 1.0},
        )
        original = Path("reference-full.wav")
        isolated = Path("reference-selected.wav")
        context = CloneContext(
            original_reference_path=original,
            model_reference_path=original,
            isolated_reference_path=isolated,
            audit_prompt_text="Complete reference text.",
            model_prompt_text="Complete reference text.",
            reference_language="en",
            target_language="en",
            effective_mode=TTSCloneMode.ULTIMATE,
        )
        observable = quality_metrics(
            quality_passed=False,
            speed_ratio=0.96,
            speed_score=0.96,
            speaker_similarity=0.7,
        )
        unavailable = quality_metrics(
            quality_passed=False,
            speed_ratio=None,
            speed_score=None,
            speaker_similarity=0.9,
        )
        synthesize = AsyncMock(
            side_effect=[
                (b"ultimate-1", "voxcpm2", 1),
                (b"ultimate-2", "voxcpm2", 1),
                (b"ultimate-3", "voxcpm2", 1),
                (b"controllable", "voxcpm2", 1),
            ]
        )
        with (
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(
                    side_effect=[observable, observable, observable, unavailable]
                ),
            ),
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                "request-id",
            )

        self.assertEqual(response.body, b"ultimate-1")
        self.assertEqual(response.headers["x-tts-speed-ratio"], "0.9600")
        self.assertEqual(response.headers["x-tts-requested-speed"], "1.2000")
        self.assertEqual(
            response.headers["x-tts-candidate-speed-ratios"],
            "0.9600,0.9600,0.9600,unavailable",
        )

    async def test_explicit_speed_calibrates_selected_candidate_to_reference_rate(
        self,
    ) -> None:
        request = TTSPublicSpeechRequest(
            text="Welcome back.",
            language="en",
            reference_audio_url="https://cdn.example/reference.wav",
            prosody={"speed": 1.2, "volume": 1.0, "pitch": 1.0},
        )
        reference = Path("reference-full.wav")
        context = CloneContext(
            original_reference_path=reference,
            model_reference_path=reference,
            audit_prompt_text="Complete reference text.",
            model_prompt_text="Complete reference text.",
            reference_language="en",
            target_language="en",
            effective_mode=TTSCloneMode.ULTIMATE,
        )
        slow = quality_metrics(
            quality_passed=False,
            speed_ratio=0.8,
            speed_score=0.8,
            duration_seconds=10.0,
        )
        slower = quality_metrics(
            quality_passed=False,
            speed_ratio=0.7,
            speed_score=0.7,
            duration_seconds=11.0,
        )
        calibrated = quality_metrics(
            quality_passed=False,
            speed_ratio=1.0,
            speed_score=1.0,
            duration_seconds=8.0,
        )
        synthesize = AsyncMock(
            side_effect=[
                (b"candidate-1", "voxcpm2", 1),
                (b"candidate-2", "voxcpm2", 1),
                (b"candidate-3", "voxcpm2", 1),
            ]
        )
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api._synthesize_enhanced_once", synthesize),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(side_effect=[slow, slower, slower, calibrated]),
            ),
            patch(
                "control_plane.api.apply_prosody_wav",
                return_value=b"calibrated",
            ) as apply_prosody,
        ):
            response = await _strict_enhanced_response(
                request,
                context,
                "request-id",
            )

        self.assertEqual(response.body, b"calibrated")
        self.assertAlmostEqual(apply_prosody.call_args.args[2].speed, 1.25)
        self.assertEqual(response.headers["x-tts-speed-calibrated"], "true")
        self.assertEqual(response.headers["x-tts-speed-correction"], "1.2500")
        self.assertEqual(response.headers["x-tts-speed-ratio"], "1.0000")

    async def test_high_confidence_exact_first_sound_is_not_rejected_on_duration_alone(self) -> None:
        transcription = {
            "text": "欢迎体验",
            "segments": [
                {
                    "words": [
                        {"start": 0.0, "end": 0.4, "word": "欢", "probability": 0.999},
                        {"start": 0.4, "end": 0.5, "word": "迎", "probability": 0.999},
                        {"start": 0.5, "end": 0.6, "word": "体验", "probability": 0.999},
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "欢迎体验",
                "zh",
                b"not-a-wav",
                None,
                None,
                1.0,
                None,
                True,
            )

        self.assertFalse(metrics["first_sound_anomaly"])
        self.assertTrue(metrics["quality_passed"])

    async def test_low_confidence_stretched_first_sound_is_rejected(self) -> None:
        transcription = {
            "text": "欢迎体验",
            "segments": [
                {
                    "words": [
                        {"start": 0.0, "end": 0.4, "word": "欢", "probability": 0.60},
                        {"start": 0.4, "end": 0.5, "word": "迎", "probability": 0.999},
                        {"start": 0.5, "end": 0.6, "word": "体验", "probability": 0.999},
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "欢迎体验",
                "zh",
                b"not-a-wav",
                None,
                None,
                1.0,
                None,
                True,
            )

        self.assertTrue(metrics["first_sound_anomaly"])
        self.assertFalse(metrics["quality_passed"])

    async def test_quality_gate_rejects_abnormally_long_final_sound(self) -> None:
        transcription = {
            "text": "abc",
            "segments": [
                {
                    "words": [
                        {"start": 0.0, "end": 0.1, "word": "a", "probability": 0.99},
                        {"start": 0.1, "end": 0.2, "word": "b", "probability": 0.99},
                        {"start": 0.2, "end": 10.9, "word": "c", "probability": 0.50},
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "abc", "en", tone_wav(10.9), None, None, 1.0, None
            )

        self.assertTrue(metrics["last_sound_anomaly"])
        self.assertTrue(metrics["longest_sound_anomaly"])
        self.assertFalse(metrics["quality_passed"])

    async def test_high_confidence_exact_long_word_is_not_a_last_sound_anomaly(self) -> None:
        transcription = {
            "text": "С возвращением.",
            "segments": [
                {
                    "words": [
                        {"start": 0.0, "end": 0.08, "word": "С", "probability": 0.99},
                        {
                            "start": 0.08,
                            "end": 0.98,
                            "word": "возвращением",
                            "probability": 0.99,
                        },
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "С возвращением.",
                "ru",
                tone_wav(1.12),
                None,
                None,
                1.0,
                None,
            )

        self.assertFalse(metrics["last_sound_anomaly"])
        self.assertTrue(metrics["quality_passed"])

    async def test_trailing_silence_is_rejected_as_silent_tail(self) -> None:
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16_000)
            target.writeframes(b"\x10\x27" * round(0.75 * 16_000))
            target.writeframes(b"\x00\x00" * round(1.37 * 16_000))
        transcription = {
            "text": "欢迎回来",
            "segments": [
                {
                    "words": [
                        {
                            "start": 0.0,
                            "end": 0.5,
                            "word": "欢迎回来",
                            "probability": 0.99,
                        }
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "欢迎回来", "zh", output.getvalue(), None, None, 1.0, None
            )

        self.assertGreater(metrics["tail_seconds"], 1.0)
        self.assertLess(metrics["tail_rms_dbfs"], -80.0)
        self.assertFalse(metrics["energetic_tail"])
        self.assertTrue(metrics["silent_tail_anomaly"])
        self.assertFalse(metrics["quality_passed"])

    async def test_speed_estimate_is_a_warning_not_a_hard_quality_failure(self) -> None:
        reference = Path(self.temporary.name) / "reference.wav"
        reference.write_bytes(tone_wav(1.0))
        transcription = {
            "text": "abcdefghij",
            "segments": [
                {
                    "words": [
                        {
                            "start": 0.0,
                            "end": 1.3,
                            "word": "abcdefghij",
                            "probability": 0.99,
                        }
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
            patch(
                "control_plane.api._speaker_similarity",
                AsyncMock(return_value=0.8),
            ),
        ):
            metrics = await _quality_metrics(
                "abcdefghij",
                "en",
                tone_wav(1.3),
                reference,
                "abcdefghij",
                1.0,
                None,
            )

        self.assertLess(metrics["speed_ratio"], 0.8)
        self.assertFalse(metrics["speed_passed"])
        self.assertTrue(metrics["quality_passed"])

    async def test_speed_estimate_is_unavailable_without_reference_transcript(self) -> None:
        transcription = {
            "text": "abcdefghij",
            "segments": [
                {
                    "words": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "word": "abcdefghij",
                            "probability": 0.99,
                        }
                    ]
                }
            ],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "abcdefghij",
                "en",
                tone_wav(1.0),
                None,
                None,
                1.2,
                None,
            )

        self.assertIsNone(metrics["target_characters_per_second"])
        self.assertIsNone(metrics["speed_ratio"])
        self.assertIsNone(metrics["speed_score"])
        self.assertTrue(metrics["speed_passed"])

    async def test_exact_content_with_energetic_tail_is_safely_trimmed(self) -> None:
        generated = tone_wav(4.0)
        before = quality_metrics(
            quality_passed=False,
            duration_seconds=4.0,
            duration_anomaly=True,
            energetic_tail=True,
            last_word_end_seconds=1.2,
            quality_score=0.7,
        )
        after = quality_metrics(duration_seconds=1.45)
        with (
            patch(
                "control_plane.api._synthesize_enhanced_once",
                AsyncMock(return_value=(generated, "voxcpm2", 1)),
            ),
            patch(
                "control_plane.api._quality_metrics",
                AsyncMock(side_effect=[before, before, before, after]),
            ),
        ):
            response = await _strict_enhanced_response(
                TTSPublicSpeechRequest(text="hello"),
                CloneContext(),
                "request-id",
            )

        self.assertEqual(response.headers["x-tts-tail-trimmed"], "true")
        self.assertLess(len(response.body), len(generated))

    def test_content_gate_scales_for_long_text(self) -> None:
        expected = "字" * 500
        actual = "词" + "字" * 499

        edits, allowed, error_rate, _, _, passed = _content_quality(expected, actual)

        self.assertEqual(edits, 1)
        self.assertEqual(allowed, 5)
        self.assertAlmostEqual(error_rate, 0.002)
        self.assertTrue(passed)

    def test_content_gate_treats_chinese_and_arabic_digits_as_equivalent(self) -> None:
        edits, _, _, _, _, passed = _content_quality("今天有三项任务", "今天有3项任务")

        self.assertEqual(edits, 0)
        self.assertTrue(passed)

    def test_content_gate_treats_simplified_and_traditional_chinese_as_phonetic_equivalents(self) -> None:
        result = _content_quality("欢迎回来", "歡迎回來")

        edits, _, _, phonetic_edits, phonetic_error_rate, passed = result
        self.assertGreater(edits, 0)
        self.assertEqual(phonetic_edits, 0)
        self.assertEqual(phonetic_error_rate, 0.0)
        self.assertTrue(passed)

    def test_content_gate_treats_japanese_kanji_and_kana_as_phonetic_equivalents(self) -> None:
        for expected, actual in (
            ("お帰りなさい", "おかえりなさい"),
            ("分かりやすい", "わかりやすい"),
        ):
            with self.subTest(expected=expected, actual=actual):
                result = _content_quality(expected, actual)
                edits, _, _, phonetic_edits, phonetic_error_rate, passed = result
                self.assertGreater(edits, 0)
                self.assertEqual(phonetic_edits, 0)
                self.assertEqual(phonetic_error_rate, 0.0)
                self.assertTrue(passed)

    async def test_traditional_asr_text_is_not_misclassified_as_extra_prefix(self) -> None:
        transcription = {
            "text": "歡迎回來",
            "segments": [{"text": "歡迎回來", "words": []}],
        }
        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch(
                "control_plane.api._transcribe_content",
                AsyncMock(return_value=transcription),
            ),
        ):
            metrics = await _quality_metrics(
                "欢迎回来",
                "zh",
                tone_wav(1.0),
                None,
                None,
                1.0,
                None,
            )

        self.assertTrue(metrics["content_passed"])
        self.assertFalse(metrics["unexpected_prefix"])
        self.assertFalse(metrics["unexpected_suffix"])
        self.assertTrue(metrics["quality_passed"])

    def test_content_gate_accepts_chinese_homophone_asr_variants(self) -> None:
        result = _content_quality("阿斯顿撒大大", "阿斯顿萨达达")

        edits, allowed, error_rate, phonetic_edits, phonetic_error_rate, passed = result
        self.assertEqual(edits, 3)
        self.assertEqual(allowed, 0)
        self.assertEqual(error_rate, 0.5)
        self.assertEqual(phonetic_edits, 0)
        self.assertEqual(phonetic_error_rate, 0.0)
        self.assertTrue(passed)

    def test_quality_asr_uses_target_script_when_language_is_auto(self) -> None:
        self.assertEqual(_quality_asr_language("你好世界", "auto"), "zh")
        self.assertEqual(_quality_asr_language("こんにちは", "auto"), "ja")
        self.assertEqual(_quality_asr_language("お帰りなさい", "auto"), "ja")
        self.assertEqual(_quality_asr_language("안녕하세요", "auto"), "ko")
        self.assertIsNone(_quality_asr_language("Hello", "auto"))
        self.assertEqual(_quality_asr_language("Hello", "en"), "en")

    async def test_ocr_urls_are_forwarded_as_temporary_local_paths(self) -> None:
        async def download(_url, directory, stem, *_args, **_kwargs):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{stem}.png"
            path.write_bytes(b"image")
            return DownloadedMedia(path, 5, "image/png")

        with (
            patch("control_plane.api.get_settings", return_value=self.settings),
            patch("control_plane.api.download_public_media_async", side_effect=download),
            patch("control_plane.api.httpx.AsyncClient", FakeHttpClient),
        ):
            response = await ocr_url_batch(
                OCRUrlBatchRequest(
                    images=[
                        OCRUrlImage(
                            image_id="front",
                            url="https://cdn.example/front.png?signature=secret",
                        )
                    ]
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(FakeHttpClient.last_json["images"][0]["image_id"], "front")
        self.assertNotIn("url", FakeHttpClient.last_json["images"][0])
        self.assertNotIn("secret", str(FakeHttpClient.last_json))
        self.assertEqual(list(self.settings.remote_input_dir.glob("ocr-*")), [])

    async def test_face_url_is_validated_before_enqueue(self) -> None:
        face_request = AsyncMock(return_value={"job_id": "job", "status": "queued"})
        with (
            patch("control_plane.api.validate_public_https_url") as validate,
            patch("control_plane.api._face_request", face_request),
        ):
            result = await create_face_url_job(
                FaceUrlJobRequest(source_uri="https://cdn.example/source.mp4")
            )

        self.assertEqual(result["status"], "queued")
        validate.assert_called_once_with("https://cdn.example/source.mp4")
        payload = face_request.await_args.args[2]
        self.assertNotIn("callback_url", payload)

    async def test_face_wait_is_validated_and_forwarded(self) -> None:
        face_request = AsyncMock(
            return_value={
                "job_id": "job",
                "status": "succeeded",
                "video_url": "https://oss.example/result.mp4",
            }
        )
        with (
            patch("control_plane.api.validate_public_https_url") as validate,
            patch("control_plane.api._face_request", face_request),
        ):
            result = await create_face_url_job_and_wait(
                FaceUrlJobRequest(source_uri="https://cdn.example/source.mp4")
            )

        self.assertEqual(result["video_url"], "https://oss.example/result.mp4")
        validate.assert_called_once_with("https://cdn.example/source.mp4")
        self.assertEqual(face_request.await_args.args[1], "/v1/face-mosaic/jobs/wait")


if __name__ == "__main__":
    unittest.main()
