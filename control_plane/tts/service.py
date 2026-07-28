from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable
from typing import Any

from ..config import Settings
from .audio import normalize_to_wav
from .base import (
    PermanentTTSProviderError,
    SynthesisResult,
    TTSProvider,
    TTSProviderError,
    TransientTTSProviderError,
)
from .providers.doubao import DoubaoProvider
from .providers.elevenlabs import ElevenLabsProvider
from .providers.voxcpm2 import VoxCPM2Provider
from .schemas import TTSSpeechRequest, TTSProviderName, VoiceProfile
from .voices import VoiceRegistry


class TTSService:
    def __init__(
        self,
        settings: Settings,
        providers: Iterable[TTSProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = VoiceRegistry(settings.tts_voice_registry_path)
        configured_providers = list(providers or self._build_providers(settings))
        self.providers = {
            provider.name: provider for provider in configured_providers
        }
        limits = {
            TTSProviderName.VOXCPM2.value: settings.voxcpm2_tts_max_concurrency,
            TTSProviderName.DOUBAO.value: settings.doubao_tts_max_concurrency,
            TTSProviderName.ELEVENLABS.value: settings.elevenlabs_tts_max_concurrency,
        }
        self._semaphores = {
            name: threading.BoundedSemaphore(max(1, limits.get(name, 1)))
            for name in self.providers
        }

    async def synthesize(self, request: TTSSpeechRequest) -> SynthesisResult:
        profile = self.registry.get(request.voice_profile_id)
        self._validate_language(profile, request.language)
        candidates = self._candidate_names(request, profile)
        transient_errors: list[str] = []
        for name in candidates:
            provider = self.providers.get(name)
            binding = profile.bindings.get(name)
            if not provider or not provider.enabled or binding is None:
                if request.provider != TTSProviderName.AUTO:
                    raise PermanentTTSProviderError(
                        f"TTS provider {name} is disabled or has no voice binding"
                    )
                continue
            try:
                raw = await self._call_provider(provider, request, binding)
            except TransientTTSProviderError as exc:
                transient_errors.append(f"{name}: {exc}")
                if request.provider != TTSProviderName.AUTO:
                    raise
                continue
            except TTSProviderError:
                raise
            audio, duration_ms = await asyncio.to_thread(
                normalize_to_wav,
                raw.audio,
                self.settings.tts_ffmpeg_bin,
                request.audio.sample_rate,
                request.audio.channels,
            )
            target = request.timing.target_duration_ms
            metadata: dict[str, Any] = dict(raw.metadata)
            if raw.provider_duration_ms is not None:
                metadata["provider_duration_ms"] = raw.provider_duration_ms
            if target is not None:
                metadata["target_duration_ms"] = target
                metadata["duration_delta_ms"] = duration_ms - target
                metadata["within_tolerance"] = (
                    abs(duration_ms - target) <= request.timing.tolerance_ms
                )
            return SynthesisResult(
                audio=audio,
                provider=raw.provider,
                audio_duration_ms=duration_ms,
                provider_request_id=raw.provider_request_id,
                metadata=metadata,
            )
        detail = "; ".join(transient_errors) or "no enabled provider has a binding"
        raise TransientTTSProviderError(f"no TTS provider succeeded: {detail}")

    def statuses(self) -> list[dict[str, Any]]:
        return [self.providers[name].status() for name in sorted(self.providers)]

    async def _call_provider(
        self,
        provider: TTSProvider,
        request: TTSSpeechRequest,
        binding: dict[str, Any],
    ):
        semaphore = self._semaphores[provider.name]
        await asyncio.to_thread(semaphore.acquire)
        try:
            return await provider.synthesize(request, binding)
        finally:
            semaphore.release()

    def _candidate_names(
        self,
        request: TTSSpeechRequest,
        profile: VoiceProfile,
    ) -> list[str]:
        if request.provider != TTSProviderName.AUTO:
            return [request.provider.value]
        if profile.fallback_order:
            return [provider.value for provider in profile.fallback_order]
        return [
            item.strip()
            for item in self.settings.tts_auto_provider_order.split(",")
            if item.strip()
        ]

    @staticmethod
    def _validate_language(profile: VoiceProfile, language: str) -> None:
        if not profile.languages:
            return
        language_lower = language.lower()
        supported = {
            item.lower()
            for item in profile.languages
        }
        if language_lower in supported:
            return
        base_language = language_lower.split("-", 1)[0]
        if base_language in {item.split("-", 1)[0] for item in supported}:
            return
        raise PermanentTTSProviderError(
            f"voice profile {profile.voice_profile_id} does not support {language}"
        )

    @staticmethod
    def _build_providers(settings: Settings) -> list[TTSProvider]:
        doubao_token = (
            settings.doubao_tts_access_token.get_secret_value()
            if settings.doubao_tts_access_token
            else None
        )
        elevenlabs_key = (
            settings.elevenlabs_tts_api_key.get_secret_value()
            if settings.elevenlabs_tts_api_key
            else None
        )
        return [
            VoxCPM2Provider(
                settings.tts_backend_url,
                settings.upstream_timeout_seconds,
                settings.voxcpm2_tts_enabled,
            ),
            DoubaoProvider(
                settings.doubao_tts_url,
                settings.doubao_tts_app_id,
                doubao_token,
                settings.doubao_tts_cluster,
                settings.upstream_timeout_seconds,
                settings.doubao_tts_enabled,
            ),
            ElevenLabsProvider(
                settings.elevenlabs_tts_base_url,
                elevenlabs_key,
                settings.elevenlabs_tts_model_id,
                settings.elevenlabs_tts_output_format,
                settings.upstream_timeout_seconds,
                settings.elevenlabs_tts_enabled,
            ),
        ]

