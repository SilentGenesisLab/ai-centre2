from __future__ import annotations

from typing import Any, BinaryIO

import httpx


class LipSyncUpstreamError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class LipSyncClient:
    def __init__(self, base_url: str, service_token: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {service_token}"}
        self.timeout = httpx.Timeout(timeout_seconds, connect=15)

    async def submit_urls(
        self,
        video_url: str,
        audio_url: str,
        face_restore: bool,
    ) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            "/v1/lipsync/jobs",
            json={
                "video_url": video_url,
                "audio_url": audio_url,
                "face_restore": face_restore,
            },
        )

    async def submit_upload(
        self,
        video_filename: str,
        video: BinaryIO,
        audio_filename: str,
        audio: BinaryIO,
        face_restore: bool,
    ) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            "/v1/lipsync/jobs/upload",
            files={
                "video": (video_filename, video, "application/octet-stream"),
                "audio": (audio_filename, audio, "application/octet-stream"),
            },
            data={"face_restore": str(face_restore).lower()},
        )

    async def status(self, job_id: str) -> dict[str, Any]:
        return await self._json_request("GET", f"/v1/lipsync/jobs/{job_id}")

    async def list_jobs(self, limit: int, state: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state
        return await self._json_request("GET", "/v1/lipsync/jobs", params=params)

    async def logs(self, job_id: str, stage: str, tail: int) -> dict[str, Any]:
        return await self._json_request(
            "GET",
            f"/v1/lipsync/jobs/{job_id}/logs",
            params={"stage": stage, "tail": tail},
        )

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return await self._json_request("POST", f"/v1/lipsync/jobs/{job_id}/cancel")

    async def video(self, job_id: str) -> tuple[bytes, dict[str, str]]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/v1/lipsync/jobs/{job_id}/video",
                    headers=self.headers,
                )
        except httpx.RequestError as exc:
            raise LipSyncUpstreamError(503, "MuseTalk backend is unavailable") from exc
        self._raise_for_response(response)
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-disposition", "content-length"}
        }
        return response.content, headers

    async def _json_request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self.headers,
                    **kwargs,
                )
        except httpx.RequestError as exc:
            raise LipSyncUpstreamError(503, "MuseTalk backend is unavailable") from exc
        self._raise_for_response(response)
        return response.json()

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json().get("detail", "MuseTalk backend request failed")
        except (ValueError, AttributeError):
            detail = "MuseTalk backend request failed"
        raise LipSyncUpstreamError(response.status_code, str(detail))
