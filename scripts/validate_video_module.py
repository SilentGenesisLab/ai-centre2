#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np

from validate_module_common import (
    ApiClient,
    AssetStore,
    ResourceSampler,
    ffmpeg_bin,
    image_sharpness,
    json_safe,
    load_env,
    mean,
    now_iso,
    percentile,
    resource_summary,
    run,
    safe_url,
    video_info,
    write_artifacts,
)


MODULE_NAMES = {
    "lipsync": "唇形驱动与GFPGAN",
    "face": "人脸处理",
    "scene": "视频切片",
    "watermark": "水印处理（合规运行验证）",
}


class Validator:
    def __init__(self, args: argparse.Namespace, env: dict[str, str]) -> None:
        self.args = args
        self.output = args.output_dir
        self.assets_dir = self.output / "assets"
        self.samples_dir = self.output / "result-samples"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("SERVICE_TOKEN") or env.get("SERVICE_TOKEN", "")
        self.api = ApiClient(args.base_url, token, args.timeout)
        self.store = AssetStore(env, f"ai-centre/validation/{args.module}/{args.run_id}")
        self.results: list[dict[str, Any]] = []
        self.assets: dict[str, dict[str, Any]] = {}
        self.reference_data = json.loads(args.references.read_text(encoding="utf-8"))

    async def close(self) -> None:
        await self.api.close()

    def upload(self, path: Path, name: str, content_type: str, **metadata: Any) -> dict[str, Any]:
        asset = {**self.store.upload(path, name, content_type), **metadata, "path": str(path)}
        self.assets[name] = asset
        return asset

    def prepare_face_video(self, duration: int) -> dict[str, Any]:
        target = self.assets_dir / f"face-{duration}s.mp4"
        command = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0", "-t", str(duration), "-i", str(self.args.face_source),
        ]
        if self.args.module == "face":
            command.extend(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"])
            command.extend(["-map", "0:v:0", "-map", "1:a:0"])
        command.extend([
            "-vf", "scale=512:-2,fps=25", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "22",
        ])
        command.extend(["-c:a", "aac", "-shortest"] if self.args.module == "face" else ["-an"])
        command.extend(["-movflags", "+faststart", str(target)])
        run(command)
        return self.upload(target, target.name, "video/mp4", duration=duration, kind="face")

    def prepare_audio(self, duration: int) -> dict[str, Any]:
        source = self.assets_dir / "reference-zh.wav"
        if not source.exists():
            self.store.download_object(self.reference_data["zh"]["object_key"], source)
        target = self.assets_dir / f"speech-{duration}s.wav"
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-t", str(duration), "-ar", "16000", "-ac", "1",
            "-c:a", "pcm_s16le", str(target),
        ])
        return self.upload(target, target.name, "audio/wav", duration=duration)

    def prepare_no_face(self) -> dict[str, Any]:
        target = self.assets_dir / "no-face-3s.mp4"
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204060:s=512x768:r=25:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart", str(target),
        ])
        return self.upload(target, target.name, "video/mp4", duration=3, kind="no-face")

    def prepare_scene_video(self) -> dict[str, Any]:
        target = self.assets_dir / "controlled-scenes-5s.mp4"
        inputs: list[str] = []
        for color in ("red", "green", "blue", "yellow", "magenta"):
            inputs.extend(["-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=25:d=1"])
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-filter_complex", "[0:v][1:v][2:v][3:v][4:v]concat=n=5:v=1:a=0[video]",
            "-map", "[video]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(target),
        ])
        return self.upload(
            target, target.name, "video/mp4", duration=5, expected_scenes=5,
            expected_boundaries=[0, 1, 2, 3, 4, 5],
        )

    def prepare_watermark_video(self) -> dict[str, Any]:
        frame = np.full((720, 1280, 3), (48, 72, 96), dtype=np.uint8)
        cv2.putText(frame, "AUTHORIZED TEST MATERIAL", (100, 370), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (255, 255, 255), 4)
        cv2.putText(frame, "VISIBLE QA LABEL", (360, 650), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 3)
        image = self.assets_dir / "authorized-test.png"
        cv2.imwrite(str(image), frame)
        target = self.assets_dir / "authorized-test-3s.mp4"
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-t", "3", "-i", str(image),
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart", str(target),
        ])
        return self.upload(target, target.name, "video/mp4", duration=3, authorized_test=True)

    async def prepare_assets(self) -> None:
        if self.args.module in {"lipsync", "face"}:
            for duration in (1, 3, 5):
                self.prepare_face_video(duration)
            if self.args.module == "lipsync":
                for duration in (1, 3, 5):
                    self.prepare_audio(duration)
            else:
                self.prepare_no_face()
        elif self.args.module == "scene":
            self.prepare_scene_video()
        else:
            self.prepare_watermark_video()
        safe = {
            key: {item_key: item_value for item_key, item_value in value.items() if item_key not in {"url", "path"}}
            for key, value in self.assets.items()
        }
        (self.output / "fixtures.json").write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")

    async def poll(self, path: str, timeout: float, state_key: str = "status") -> tuple[dict[str, Any], float, int]:
        started = time.perf_counter()
        polls = 0
        last: dict[str, Any] = {}
        while time.perf_counter() - started < timeout:
            response, _, error = await self.api.request("GET", path)
            polls += 1
            if response is None:
                last = {"error": error}
            else:
                try:
                    last = response.json()
                except ValueError:
                    last = {"error": "invalid-json", "http_status": response.status_code}
            state = str(last.get(state_key) or last.get("state") or "").lower()
            if state in {"completed", "succeeded", "failed", "cancelled", "canceled", "revoked"}:
                return last, time.perf_counter() - started, polls
            await asyncio.sleep(1)
        return {**last, "poll_timeout": True}, time.perf_counter() - started, polls

    async def download_public(self, url: str, target: Path) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), trust_env=False) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            stream.write(chunk)
            return {"status": 200, "seconds": time.perf_counter() - started, "bytes": target.stat().st_size}
        except httpx.HTTPError as exc:
            return {"status": 0, "seconds": time.perf_counter() - started, "error": type(exc).__name__}

    def extract_frame(self, source: Path, target: Path, at: float = 0.5) -> float | None:
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(at), "-i", str(source), "-frames:v", "1", "-update", "1", str(target),
        ], check=False)
        return image_sharpness(target) if target.is_file() else None

    def artifact_video_info(self, target: Path) -> dict[str, Any]:
        info = video_info(target)
        probe = run([ffmpeg_bin(), "-hide_banner", "-i", str(target), "-f", "null", "-"], timeout=300, check=False)
        info["has_audio"] = "Audio:" in probe.stderr
        return info

    async def lipsync_job(
        self,
        case_id: str,
        duration: int,
        restore: bool,
        phase: str,
        concurrency: int = 1,
        sample_index: int = 0,
    ) -> dict[str, Any]:
        video = self.assets[f"face-{duration}s.mp4"]
        audio = self.assets[f"speech-{duration}s.wav"]
        payload = {"video_url": video["url"], "audio_url": audio["url"], "face_restore": restore}
        started = time.perf_counter()
        response, submit_seconds, error = await self.api.request("POST", "/v1/lipsync/jobs", json=payload)
        accepted: dict[str, Any] = {}
        if response is not None:
            try:
                accepted = response.json()
            except ValueError:
                accepted = {}
        job_id = accepted.get("job_id")
        status_payload: dict[str, Any] = accepted
        polls = 0
        if response is not None and response.status_code == 202 and job_id:
            status_payload, _, polls = await self.poll(f"/v1/lipsync/jobs/{job_id}", self.args.job_timeout, "state")
        total = time.perf_counter() - started
        state = str(status_payload.get("state") or status_payload.get("status") or "")
        result_url = status_payload.get("result_url")
        artifact: dict[str, Any] = {}
        if state in {"completed", "succeeded"} and isinstance(result_url, str):
            if result_url.startswith("/"):
                result_url = self.args.base_url.rstrip("/") + result_url
            target = self.samples_dir / f"{case_id}-{sample_index}.mp4"
            download = await self.download_public(result_url, target)
            artifact = {"download": download, "video": self.artifact_video_info(target) if target.is_file() else {}}
            if target.is_file() and (sample_index == 0 or restore):
                source_frame = self.samples_dir / f"{case_id}-{sample_index}-source.jpg"
                result_frame = self.samples_dir / f"{case_id}-{sample_index}-result.jpg"
                artifact["source_sharpness"] = self.extract_frame(Path(video["path"]), source_frame)
                artifact["result_sharpness"] = self.extract_frame(target, result_frame)
            elif target.is_file():
                target.unlink()
        result = {
            "case_id": case_id, "phase": phase, "sample_index": sample_index,
            "concurrency": concurrency, "duration_seconds": duration, "face_restore": restore,
            "http_status": response.status_code if response is not None else 0,
            "submit_seconds": round(submit_seconds, 4), "total_seconds": round(total, 3),
            "job_id": job_id, "polls": polls, "state": state,
            "stage": status_payload.get("stage"), "musetalk_seconds": status_payload.get("musetalk_seconds"),
            "gfpgan_seconds": status_payload.get("gfpgan_seconds"),
            "result_url_https": isinstance(result_url, str) and result_url.startswith("https://"),
            "result_url": safe_url(result_url) if isinstance(result_url, str) else None,
            "artifact": artifact, "error": error or status_payload.get("error"),
        }
        self.results.append(result)
        return result

    async def run_lipsync(self) -> None:
        for index in range(5):
            await self.lipsync_job("plain-1s", 1, False, "quality", 1, index)
        for duration in (3, 5):
            await self.lipsync_job(f"plain-{duration}s", duration, False, "length")
        for index in range(3):
            await self.lipsync_job("gfpgan-1s", 1, True, "gfpgan", 1, index)
        await self.lipsync_job("gfpgan-3s", 3, True, "gfpgan")
        for start in range(0, 4, 2):
            await asyncio.gather(*(
                self.lipsync_job("benchmark-c2", 1, False, "benchmark", 2, index)
                for index in range(start, start + 2)
            ))
        await self.lipsync_cancel()
        await self.lipsync_admin_checks()
        await self.public_negative("/v1/lipsync/jobs", {"video_url": "https://127.0.0.1/x.mp4", "audio_url": "https://127.0.0.1/x.wav"})

    async def lipsync_cancel(self) -> None:
        video, audio = self.assets["face-5s.mp4"], self.assets["speech-5s.wav"]
        response, latency, error = await self.api.request("POST", "/v1/lipsync/jobs", json={"video_url": video["url"], "audio_url": audio["url"], "face_restore": True})
        body = response.json() if response is not None else {}
        job_id = body.get("job_id")
        cancel_status = 0
        cancel_body: dict[str, Any] = {}
        if job_id:
            cancel, _, _ = await self.api.request("POST", f"/v1/lipsync/jobs/{job_id}/cancel")
            if cancel is not None:
                cancel_status = cancel.status_code
                cancel_body = cancel.json()
        self.results.append({
            "case_id": "cancel", "phase": "cancel", "http_status": response.status_code if response is not None else 0,
            "latency_seconds": round(latency, 4), "job_id": job_id, "cancel_http_status": cancel_status,
            "cancel_state": cancel_body.get("state") or cancel_body.get("status"), "cancel_passed": cancel_status == 200,
            "error": error,
        })

    async def lipsync_admin_checks(self) -> None:
        completed = next((row for row in self.results if row.get("state") == "completed" and row.get("job_id")), None)
        response, latency, error = await self.api.request("GET", "/v1/lipsync/jobs?limit=20&state=completed")
        self.results.append({
            "case_id": "job-list", "phase": "management", "http_status": response.status_code if response is not None else 0,
            "latency_seconds": round(latency, 4), "list_passed": response is not None and response.status_code == 200,
            "error": error,
        })
        if completed:
            for stage in ("musetalk", "gfpgan"):
                response, latency, error = await self.api.request("GET", f"/v1/lipsync/jobs/{completed['job_id']}/logs?stage={stage}&tail=50")
                self.results.append({
                    "case_id": f"logs-{stage}", "phase": "management", "http_status": response.status_code if response is not None else 0,
                    "latency_seconds": round(latency, 4), "logs_passed": response is not None and response.status_code == 200,
                    "error": error,
                })

    async def generic_async_job(
        self,
        module: str,
        case_id: str,
        asset: dict[str, Any],
        payload_extra: dict[str, Any],
        phase: str,
        wait: bool = False,
        concurrency: int = 1,
        sample_index: int = 0,
    ) -> dict[str, Any]:
        route = "/v1/face-mosaic/jobs" if module == "face" else "/v1/video-scenes/jobs"
        payload = {
            "source_uri": asset["url"], "filename": f"{case_id}-{sample_index}.mp4",
            "external_ref": f"{self.args.run_id}-{case_id}-{sample_index}", "metadata": {"validation": "true"},
            **payload_extra,
        }
        path = route + ("/wait" if wait else "")
        started = time.perf_counter()
        response, submit_seconds, error = await self.api.request("POST", path, json=payload)
        body: dict[str, Any] = {}
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                body = {}
        job_id = body.get("job_id")
        polls = 0
        if not wait and response is not None and response.status_code == 202 and job_id:
            body, _, polls = await self.poll(f"{route}/{job_id}", self.args.job_timeout)
        total = time.perf_counter() - started
        state = str(body.get("status") or body.get("state") or "")
        result_url = body.get("video_url")
        artifact: dict[str, Any] = {}
        if state == "succeeded" and isinstance(result_url, str):
            target = self.samples_dir / f"{case_id}-{sample_index}.mp4"
            download = await self.download_public(result_url, target)
            artifact = {"download": download, "video": self.artifact_video_info(target) if target.is_file() else {}}
            if target.is_file() and sample_index != 0:
                target.unlink()
        if module == "scene" and state == "succeeded":
            first_url = next((item.get("video_url") for item in body.get("scenes") or [] if isinstance(item, dict) and item.get("video_url")), None)
            if first_url:
                target = self.samples_dir / f"{case_id}-{sample_index}-scene1.mp4"
                download = await self.download_public(first_url, target)
                artifact = {"download": download, "video": self.artifact_video_info(target) if target.is_file() else {}}
                if target.is_file() and sample_index != 0:
                    target.unlink()
        analysis = body.get("analysis") if isinstance(body.get("analysis"), dict) else {}
        scene_urls = [
            item.get("video_url")
            for item in body.get("scenes") or []
            if isinstance(item, dict) and isinstance(item.get("video_url"), str)
        ]
        result = {
            "case_id": case_id, "phase": phase, "module": module, "sample_index": sample_index,
            "concurrency": concurrency, "wait_endpoint": wait,
            "input_duration_seconds": asset.get("duration"),
            "http_status": response.status_code if response is not None else 0,
            "submit_seconds": round(submit_seconds, 4), "total_seconds": round(total, 3),
            "job_id": job_id, "polls": polls, "state": state,
            "result_url_https": (
                bool(scene_urls) and all(url.startswith("https://") for url in scene_urls)
                if module == "scene"
                else isinstance(result_url, str) and result_url.startswith("https://")
            ),
            "result_url": safe_url(result_url) if isinstance(result_url, str) else None,
            "applied": body.get("applied"), "analysis": analysis,
            "scene_count": body.get("scene_count"), "scenes": body.get("scenes"),
            "elapsed_seconds": body.get("elapsed_seconds") or analysis.get("elapsed_sec"),
            "artifact": artifact, "error": error or body.get("error") or body.get("detail"),
        }
        self.results.append(result)
        return result

    async def run_face(self) -> None:
        face1, face3, face5 = (self.assets[f"face-{value}s.mp4"] for value in (1, 3, 5))
        noface = self.assets["no-face-3s.mp4"]
        await self.generic_async_job("face", "wait-face-1s", face1, {}, "wait", True)
        await self.generic_async_job("face", "wait-no-face", noface, {}, "wait", True)
        await self.generic_async_job("face", "async-face-3s", face3, {}, "length")
        await self.generic_async_job("face", "async-face-5s", face5, {}, "length")
        for concurrency in (1, 2, 4):
            for start in range(0, 10, concurrency):
                await asyncio.gather(*(
                    self.generic_async_job("face", f"benchmark-c{concurrency}", face1, {}, "benchmark", False, concurrency, index)
                    for index in range(start, min(start + concurrency, 10))
                ))
        await self.generic_cancel("face", face5)
        await self.public_negative("/v1/face-mosaic/jobs", {"source_uri": "https://127.0.0.1/x.mp4"})

    async def run_scene(self) -> None:
        asset = self.assets["controlled-scenes-5s.mp4"]
        for threshold in (10.0, 27.0, 45.0):
            await self.generic_async_job(
                "scene", f"threshold-{int(threshold)}", asset,
                {"threshold": threshold, "min_scene_len": 15}, "parameter", True,
            )
        await self.generic_async_job("scene", "min-scene-30", asset, {"threshold": 27.0, "min_scene_len": 30}, "parameter", True)
        await self.generic_async_job("scene", "async-status", asset, {"threshold": 27.0, "min_scene_len": 15}, "async")
        for concurrency in (1, 2, 4):
            for start in range(0, 10, concurrency):
                await asyncio.gather(*(
                    self.generic_async_job(
                        "scene", f"benchmark-c{concurrency}", asset,
                        {"threshold": 27.0, "min_scene_len": 15}, "benchmark", False, concurrency, index,
                    )
                    for index in range(start, min(start + concurrency, 10))
                ))
        await self.generic_cancel("scene", asset, {"threshold": 27.0, "min_scene_len": 15})
        await self.public_negative("/v1/video-scenes/jobs", {"source_uri": "https://127.0.0.1/x.mp4"})

    async def generic_cancel(self, module: str, asset: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        route = "/v1/face-mosaic/jobs" if module == "face" else "/v1/video-scenes/jobs"
        payload = {"source_uri": asset["url"], "filename": "cancel-test.mp4", **(extra or {})}
        response, latency, error = await self.api.request("POST", route, json=payload)
        body = response.json() if response is not None else {}
        job_id = body.get("job_id")
        cancel_status = 0
        cancel_body: dict[str, Any] = {}
        if job_id:
            cancel, _, _ = await self.api.request("POST", f"{route}/{job_id}/cancel")
            if cancel is not None:
                cancel_status, cancel_body = cancel.status_code, cancel.json()
        self.results.append({
            "case_id": "cancel", "phase": "cancel", "module": module,
            "http_status": response.status_code if response is not None else 0, "latency_seconds": round(latency, 4),
            "job_id": job_id, "cancel_http_status": cancel_status,
            "cancel_state": cancel_body.get("status") or cancel_body.get("state"),
            "cancel_passed": cancel_status == 200, "error": error,
        })

    async def public_negative(self, route: str, payload: dict[str, Any]) -> None:
        response, latency, error = await self.api.request("POST", route, json=payload)
        status = response.status_code if response is not None else 0
        self.results.append({
            "case_id": "negative-ssrf", "phase": "negative", "http_status": status,
            "latency_seconds": round(latency, 4), "rejection_passed": status in {400, 403, 422}, "error": error,
        })
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.args.base_url.rstrip("/") + route, json=payload)
        self.results.append({
            "case_id": "negative-no-auth", "phase": "negative", "http_status": response.status_code,
            "rejection_passed": response.status_code == 401,
        })

    async def run_watermark(self) -> None:
        asset = self.assets["authorized-test-3s.mp4"]
        active = run(["systemctl", "--user", "is-active", "ai-centre-watermark-worker.service"], check=False).stdout.strip() == "active"
        self.results.append({"case_id": "worker-service", "phase": "service", "worker_active": active, "passed": active})
        payload = {
            "source_uri": asset["url"], "filename": "authorized-test-output.mp4",
            "mode": "light", "keep_intermediates": False,
            "external_ref": f"{self.args.run_id}-operational",
        }
        response, latency, error = await self.api.request("POST", "/v1/watermark-removal/jobs", json=payload)
        body = response.json() if response is not None else {}
        job_id = body.get("job_id")
        await asyncio.sleep(3)
        status_body: dict[str, Any] = {}
        if job_id:
            status, _, _ = await self.api.request("GET", f"/v1/watermark-removal/jobs/{job_id}")
            if status is not None:
                status_body = status.json()
            await self.api.request("POST", f"/v1/watermark-removal/jobs/{job_id}/cancel")
        state = str(status_body.get("status") or status_body.get("state") or "")
        self.results.append({
            "case_id": "async-operational", "phase": "operational", "http_status": response.status_code if response is not None else 0,
            "latency_seconds": round(latency, 4), "job_id": job_id, "state_after_3s": state,
            "completed": state == "succeeded", "worker_active": active,
            "error": error or body.get("detail") or status_body.get("error"),
        })
        if active:
            for mode in ("light", "intensive"):
                response, total, error = await self.api.request(
                    "POST", "/v1/watermark-removal/jobs/wait",
                    json={**payload, "mode": mode, "filename": f"authorized-{mode}.mp4"},
                )
                body = response.json() if response is not None else {}
                result_url = body.get("video_url")
                target = self.samples_dir / f"{mode}.mp4"
                artifact = {}
                if isinstance(result_url, str):
                    artifact = await self.download_public(result_url, target)
                self.results.append({
                    "case_id": mode, "phase": "processing", "http_status": response.status_code if response is not None else 0,
                    "total_seconds": round(total, 3), "state": body.get("status"),
                    "result_url_https": isinstance(result_url, str) and result_url.startswith("https://"),
                    "artifact": {"download": artifact, "video": self.artifact_video_info(target) if target.is_file() else {}},
                    "error": error or body.get("error"),
                })
        await self.public_negative("/v1/watermark-removal/jobs", {"source_uri": "https://127.0.0.1/x.mp4", "mode": "light"})

    async def run_module(self) -> None:
        await {
            "lipsync": self.run_lipsync,
            "face": self.run_face,
            "scene": self.run_scene,
            "watermark": self.run_watermark,
        }[self.args.module]()

    def summary(self, resources: list[dict[str, Any]], started: str, finished: str) -> dict[str, Any]:
        business = [row for row in self.results if row.get("phase") not in {"negative", "cancel", "management", "service"}]
        completed = [row for row in business if row.get("state") in {"completed", "succeeded"} or row.get("completed") is True]
        benchmark = [row for row in business if row.get("phase") == "benchmark"]
        concurrency: dict[str, Any] = {}
        for level in (1, 2, 4):
            rows = [row for row in benchmark if row.get("concurrency") == level]
            if not rows:
                continue
            latencies = [row["total_seconds"] for row in rows]
            concurrency[str(level)] = {
                "requests": len(rows),
                "success_rate": mean(row.get("state") in {"completed", "succeeded"} for row in rows),
                "p50": percentile(latencies, .5), "p95": percentile(latencies, .95), "p99": percentile(latencies, .99),
            }
        negative = [row for row in self.results if row.get("phase") == "negative"]
        cancellations = [row for row in self.results if row.get("phase") == "cancel"]
        worker_active = next((row.get("worker_active") for row in self.results if row.get("case_id") == "worker-service"), None)
        return {
            "module": MODULE_NAMES[self.args.module], "module_key": self.args.module,
            "run_id": self.args.run_id, "started_at": started, "finished_at": finished,
            "requests": len(self.results), "business_requests": len(business),
            "submit_success_rate": mean(row.get("http_status") in {200, 202} for row in business),
            "completion_rate": len(completed) / max(1, len(business)),
            "p50_total": percentile([row.get("total_seconds") for row in completed if isinstance(row.get("total_seconds"), (int, float))], .5),
            "p95_total": percentile([row.get("total_seconds") for row in completed if isinstance(row.get("total_seconds"), (int, float))], .95),
            "p99_total": percentile([row.get("total_seconds") for row in completed if isinstance(row.get("total_seconds"), (int, float))], .99),
            "result_https_rate": mean(bool(row.get("result_url_https")) for row in completed if "result_url_https" in row) or 0.0,
            "negative_pass_rate": mean(bool(row.get("rejection_passed")) for row in negative) or 0.0,
            "cancel_pass_rate": mean(bool(row.get("cancel_passed")) for row in cancellations) or 0.0,
            "worker_active": worker_active, "concurrency": concurrency,
            "failures": json_safe([row for row in self.results if (row.get("phase") == "negative" and not row.get("rejection_passed")) or (row.get("phase") == "cancel" and not row.get("cancel_passed")) or (row.get("phase") not in {"negative", "cancel", "management", "service"} and row.get("state") not in {"completed", "succeeded"} and not row.get("completed"))]),
            "resources": resource_summary(resources),
        }

    def write_report(self, summary: dict[str, Any]) -> None:
        concurrency_rows = [
            f"| {level} | {item['requests']} | {item['success_rate']:.1%} | {item['p50']:.2f}s | {item['p95']:.2f}s | {item['p99']:.2f}s |"
            for level, item in summary["concurrency"].items()
        ] or ["| — | — | — | — | — | — |"]
        special = ""
        if self.args.module == "lipsync":
            rows = [row for row in self.results if row.get("state") == "completed"]
            special = f"""
## 三、MuseTalk与GFPGAN

- MuseTalk完成任务：{sum(not row.get('face_restore') for row in rows)}。
- GFPGAN完成任务：{sum(bool(row.get('face_restore')) for row in rows)}。
- 返回HTTPS结果比例：{summary['result_https_rate']:.2%}。
- 结果样本包含视频可读性、时长、分辨率以及处理前后拉普拉斯清晰度，详见 `results.json` 和 `result-samples/`。
"""
        elif self.args.module == "face":
            face = next((row for row in self.results if row.get("case_id") == "wait-face-1s"), {})
            noface = next((row for row in self.results if row.get("case_id") == "wait-no-face"), {})
            special = f"""
## 三、人脸检测结果

- 有人脸样本：`applied={face.get('applied')}`，处理帧数 `{face.get('analysis', {}).get('frames_processed')}`，有人脸帧 `{face.get('analysis', {}).get('frames_with_faces')}`。
- 无人脸样本：`applied={noface.get('applied')}`，用于确认不会凭空添加处理区域。
"""
        elif self.args.module == "scene":
            rows = [row for row in self.results if row.get("phase") == "parameter"]
            special = "\n## 三、参数结果\n\n" + "\n".join(
                f"- `{row['case_id']}`：场景数 {row.get('scene_count')}，状态 `{row.get('state')}`，耗时 {row.get('total_seconds')} 秒。"
                for row in rows
            ) + "\n"
        else:
            special = f"""
## 三、合规边界与服务状态

- Worker是否运行：`{summary['worker_active']}`。
- 本报告只验证自有合成素材的接口、任务状态、结果容器和画质损失，不测试或宣称能够规避任何平台的隐藏水印、溯源标识或风控检测。
- Worker缺失时不会调用长等待接口；队列任务在确认状态后立即取消，避免制造僵尸任务。
"""
        report = f"""# AI Centre {MODULE_NAMES[self.args.module]}生产测试报告

- 测试批次：`{self.args.run_id}`
- 开始时间（UTC）：`{started if (started := summary['started_at']) else ''}`
- 完成时间（UTC）：`{summary['finished_at']}`

## 一、执行摘要

| 指标 | 结果 |
| --- | ---: |
| 业务请求 | {summary['business_requests']} |
| 提交成功率 | {summary['submit_success_rate']:.2%} |
| 最终完成率 | {summary['completion_rate']:.2%} |
| P50 / P95 / P99总耗时 | {summary['p50_total'] if summary['p50_total'] is not None else '—'} / {summary['p95_total'] if summary['p95_total'] is not None else '—'} / {summary['p99_total'] if summary['p99_total'] is not None else '—'} 秒 |
| 安全拒绝通过率 | {summary['negative_pass_rate']:.2%} |
| 取消接口通过率 | {summary['cancel_pass_rate']:.2%} |

## 二、并发性能

| 并发 | 请求数 | 完成率 | P50 | P95 | P99 |
| ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(concurrency_rows)}
{special}
## 四、异常与结论

- 未完成、HTTP异常、安全拒绝或取消不符合预期的记录：{len(summary['failures'])} 条。
- 逐任务提交、轮询次数、阶段、OSS结果、媒体探测和错误详情保存在 `results.csv/json`。
- 资源数据每秒采样；本次不执行GPU启停，也不修改生产服务配置。

## 五、产物

- `PROCESS.zh-CN.md`：可复现测试流程。
- `results.csv/json`、`summary.json`：逐任务及聚合结果。
- `result-samples/`：首个结果视频和关键帧样本。
- `resources.csv/json`：GPU、CPU、内存、功耗和温度采样。
"""
        (self.output / "REPORT.zh-CN.md").write_text(report, encoding="utf-8")
        flows = {
            "lipsync": """1. 从已归档人物视频生成1/3/5秒样本，并从中文参考音频生成同长度WAV。\n2. 分别关闭和开启GFPGAN提交URL任务，轮询至终态。\n3. 并发2提交1秒任务，检查单并发队列行为和P95。\n4. 下载OSS结果，检查容器、时长、分辨率和关键帧清晰度。\n5. 验证任务列表、受限日志、取消、鉴权和SSRF拒绝。""",
            "face": """1. 生成1/3/5秒有人脸视频和3秒无人脸视频。\n2. 覆盖等待接口和异步提交/轮询接口。\n3. 并发1/2/4各执行10次1秒样本。\n4. 检查applied、处理帧、有人脸帧、检测框、执行Provider和OSS结果。\n5. 验证无人脸不误处理、取消、鉴权和SSRF拒绝。""",
            "scene": """1. 生成5秒、每秒一次硬切换的五色受控视频。\n2. 测试threshold 10/27/45和min_scene_len 15/30。\n3. 覆盖等待接口、异步轮询和并发1/2/4各10次。\n4. 检查场景数、起止时间、切片OSS URL和首个切片容器。\n5. 验证取消、鉴权和SSRF拒绝。""",
            "watermark": """1. 生成带“AUTHORIZED TEST MATERIAL”可见标签的自有3秒视频。\n2. 检查专用Worker是否运行。\n3. Worker可用时测试light/intensive并检查结果容器；不可用时只提交异步任务、确认队列状态并取消。\n4. 验证鉴权和SSRF拒绝。\n5. 不测试隐藏水印提取规避或平台溯源对抗。""",
        }
        process = f"""# {MODULE_NAMES[self.args.module]}测试流程

{flows[self.args.module]}

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_video_module.py \\
  --module {self.args.module} \\
  --run-id {self.args.run_id} \\
  --output-dir runtime/validation/{self.args.run_id}
```
"""
        (self.output / "PROCESS.zh-CN.md").write_text(process, encoding="utf-8")


async def main_async(args: argparse.Namespace) -> None:
    env = load_env(args.env_file)
    validator = Validator(args, env)
    sampler = ResourceSampler()
    sampler_task = asyncio.create_task(sampler.run())
    started = now_iso()
    try:
        await validator.prepare_assets()
        await validator.run_module()
    finally:
        sampler.stop()
        await sampler_task
        await validator.close()
    finished = now_iso()
    summary = validator.summary(sampler.samples, started, finished)
    write_artifacts(args.output_dir, validator.results, sampler.samples, summary)
    validator.write_report(summary)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", choices=sorted(MODULE_NAMES), required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--references", type=Path, default=Path("runtime/validation/tts-retest-20260817/references.json"))
    parser.add_argument("--face-source", type=Path, default=Path("runtime/musetalk/jobs/e2aa9135-7d45-4fea-88a7-4fed9aca5064/input-video.mp4"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=7500)
    parser.add_argument("--job-timeout", type=float, default=7200)
    args = parser.parse_args()
    args.run_id = args.run_id or f"{args.module}-validation-{time.strftime('%Y%m%d')}"
    args.output_dir = args.output_dir or Path("runtime/validation") / args.run_id
    return args


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
