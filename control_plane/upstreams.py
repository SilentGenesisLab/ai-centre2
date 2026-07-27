from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class AudioUpstreams:
    def __init__(
        self,
        asr_base_url: str,
        tts_base_url: str,
        timeout_seconds: float,
    ) -> None:
        self.asr_base_url = asr_base_url.rstrip("/")
        self.tts_base_url = tts_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=15)

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as client:
            return {
                "asr": await self._health(client, self.asr_base_url),
                "tts": await self._health(client, self.tts_base_url),
            }

    async def transcribe(
        self,
        filename: str,
        content: bytes,
        language: str | None,
        beam_size: int,
    ) -> dict[str, Any]:
        data: dict[str, str] = {"beam_size": str(beam_size)}
        if language:
            data["language"] = language
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.asr_base_url}/asr",
                data=data,
                files={"file": (filename, content, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()

    async def synthesize(
        self,
        payload: dict[str, Any],
    ) -> tuple[bytes, str, dict[str, str]]:
        has_reference = bool(
            payload.get("reference_wav_path") or payload.get("prompt_wav_path")
        )
        endpoint = "clone_path" if has_reference else "tts"
        if not has_reference:
            payload = {
                key: payload[key]
                for key in ("text", "cfg_value", "inference_timesteps")
                if key in payload
            }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.tts_base_url}/{endpoint}",
                json=payload,
            )
        response.raise_for_status()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-disposition", "x-elapsed-seconds", "x-audio-duration"}
        }
        return response.content, response.headers.get("content-type", "audio/wav"), headers

    @staticmethod
    async def _health(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
        try:
            response = await client.get(f"{base_url}/health")
            response.raise_for_status()
            return {
                "status": "ok",
                "url": base_url,
                "details": response.json(),
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "url": base_url,
                "error": f"{type(exc).__name__}: {exc}",
            }
