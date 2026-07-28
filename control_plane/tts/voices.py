from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .schemas import TTSProviderName, VoiceProfile


class VoiceProfileNotFound(KeyError):
    pass


class VoiceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_default()

    def list(self) -> list[VoiceProfile]:
        with self._lock:
            return list(self._read().values())

    def get(self, voice_profile_id: str) -> VoiceProfile:
        with self._lock:
            profiles = self._read()
            try:
                return profiles[voice_profile_id]
            except KeyError as exc:
                raise VoiceProfileNotFound(voice_profile_id) from exc

    def put(self, profile: VoiceProfile) -> VoiceProfile:
        with self._lock:
            profiles = self._read()
            current = profiles.get(profile.voice_profile_id)
            if current and profile.version <= current.version:
                profile = profile.model_copy(update={"version": current.version + 1})
            profiles[profile.voice_profile_id] = profile
            self._write(profiles)
            return profile

    def _ensure_default(self) -> None:
        with self._lock:
            if self.path.exists():
                return
            default = VoiceProfile(
                voice_profile_id="default",
                display_name="Default VoxCPM2 voice",
                languages=[],
                bindings={TTSProviderName.VOXCPM2.value: {}},
                fallback_order=[TTSProviderName.VOXCPM2],
            )
            self._write({default.voice_profile_id: default})

    def _read(self) -> dict[str, VoiceProfile]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {
            item["voice_profile_id"]: VoiceProfile.model_validate(item)
            for item in raw["profiles"]
        }

    def _write(self, profiles: dict[str, VoiceProfile]) -> None:
        payload = {
            "schema_version": 1,
            "profiles": [
                profile.model_dump(mode="json")
                for profile in sorted(
                    profiles.values(),
                    key=lambda item: item.voice_profile_id,
                )
            ],
        }
        handle, temp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

