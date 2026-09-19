#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import statistics
import subprocess
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import httpx
import imageio_ffmpeg
import oss2


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def mean(values: Iterable[float]) -> float | None:
    items = [float(value) for value in values]
    return statistics.fmean(items) if items else None


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(character for character in normalized if character.isalnum())


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).lower(), re.UNICODE)


def edit_distance(left: list[str] | str, right: list[str] | str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_value in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_value != right_value),
            ))
        previous = current
    return previous[-1]


def text_errors(expected: str, actual: str, spaced_language: bool = False) -> tuple[float, float | None]:
    expected_chars = normalize_text(expected)
    actual_chars = normalize_text(actual)
    cer = edit_distance(expected_chars, actual_chars) / max(1, len(expected_chars))
    if not spaced_language:
        return cer, None
    expected_words = words(expected)
    actual_words = words(actual)
    return cer, edit_distance(expected_words, actual_words) / max(1, len(expected_words))


def safe_url(value: str | None) -> str | None:
    if not value:
        return value
    try:
        split = urlsplit(value)
    except ValueError:
        return "[invalid-url]"
    return urlunsplit((split.scheme, split.netloc, split.path, "", ""))


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"authorization", "cookie", "token", "service_token", "api_key"}:
                result[str(key)] = "***"
            elif lowered.endswith(("_url", "_uri")) and isinstance(item, str):
                result[str(key)] = safe_url(item)
            else:
                result[str(key)] = json_safe(item)
        return result
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def run(command: list[str], timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ffmpeg_bin() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_info(path: Path) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return {"readable": False, "bytes": path.stat().st_size if path.exists() else 0}
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    capture.release()
    return {
        "readable": frames > 0 and fps > 0 and width > 0 and height > 0,
        "bytes": path.stat().st_size,
        "fps": round(fps, 3),
        "frames": frames,
        "width": width,
        "height": height,
        "duration_seconds": round(frames / fps, 3) if fps else 0,
    }


def image_sharpness(path: Path) -> float | None:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    return float(cv2.Laplacian(image, cv2.CV_64F).var())


def flatten_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    fieldnames: list[str] = []
    seen: set[str] = set()
    flattened: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(json_safe(value), ensure_ascii=False, separators=(",", ":"))
            item[key] = value
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
        flattened.append(item)
    return fieldnames, flattened


def write_artifacts(
    output_dir: Path,
    results: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("results.json", json_safe(results)),
        ("resources.json", resources),
        ("summary.json", json_safe(summary)),
    ):
        (output_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    for name, rows in (("results.csv", results), ("resources.csv", resources)):
        fields, flattened = flatten_rows(rows)
        with (output_dir / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(flattened)


class AssetStore:
    REQUIRED = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET")

    def __init__(self, env: dict[str, str], prefix: str) -> None:
        missing = [name for name in self.REQUIRED if not env.get(name)]
        if missing:
            raise RuntimeError(f"OSS configuration is incomplete: {', '.join(missing)}")
        self.bucket = oss2.Bucket(
            oss2.Auth(env["OSS_ACCESS_KEY_ID"], env["OSS_ACCESS_KEY_SECRET"]),
            env["OSS_ENDPOINT"],
            env["OSS_BUCKET"],
        )
        self.public_base = env.get("OSS_PUBLIC_BASE_URL") or (
            f"https://{env['OSS_BUCKET']}.{env['OSS_ENDPOINT']}"
        )
        self.prefix = prefix.strip("/")

    def url(self, object_key: str) -> str:
        return self.public_base.rstrip("/") + "/" + quote(object_key, safe="/")

    def upload(self, path: Path, name: str, content_type: str) -> dict[str, Any]:
        object_key = f"{self.prefix}/{name}"
        with path.open("rb") as stream:
            self.bucket.put_object(object_key, stream, headers={"Content-Type": content_type})
        return {
            "object_key": object_key,
            "url": self.url(object_key),
            "bytes": path.stat().st_size,
            "content_type": content_type,
        }

    def download_object(self, object_key: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.bucket.get_object_to_file(object_key, str(target))


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 900) -> None:
        if not token:
            raise RuntimeError("SERVICE_TOKEN is not configured")
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout, connect=20),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> tuple[httpx.Response | None, float, str | None]:
        started = time.perf_counter()
        try:
            response = await self.client.request(method, self.base_url + path, **kwargs)
            return response, time.perf_counter() - started, None
        except httpx.HTTPError as exc:
            return None, time.perf_counter() - started, type(exc).__name__

    async def download(self, url: str, target: Path) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with self.client.stream("GET", url) as response:
                response.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        stream.write(chunk)
            return {"status": 200, "seconds": time.perf_counter() - started, "bytes": target.stat().st_size}
        except httpx.HTTPError as exc:
            return {"status": 0, "seconds": time.perf_counter() - started, "error": type(exc).__name__}


class ResourceSampler:
    UNITS = {
        "control": "ai-centre-control.service",
        "asr": "ai-centre-asr-gpu0.service",
        "musetalk": "ai-centre-musetalk.service",
        "ocr": "ai-centre-ocr-worker@1.service",
        "face": "ai-centre-face-worker-gpu1.service",
        "scene": "ai-centre-scene-worker.service",
        "watermark": "ai-centre-watermark-worker.service",
    }

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._previous_cpu: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _command(command: list[str]) -> str:
        return run(command, timeout=10, check=False).stdout.strip()

    def _sample_sync(self) -> dict[str, Any]:
        now = time.monotonic()
        sample: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
        output = self._command([
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
            "--format=csv,noheader,nounits",
        ])
        for row in output.splitlines():
            columns = [column.strip() for column in row.split(",")]
            if len(columns) != 7:
                continue
            index = columns[0]
            sample[f"gpu{index}_name"] = columns[1]
            for suffix, value in zip(
                ("util_percent", "memory_used_mib", "memory_total_mib", "power_w", "temperature_c"),
                columns[2:],
            ):
                try:
                    sample[f"gpu{index}_{suffix}"] = float(value)
                except ValueError:
                    sample[f"gpu{index}_{suffix}"] = None
        for label, unit in self.UNITS.items():
            properties = dict(
                line.split("=", 1)
                for line in self._command([
                    "systemctl", "--user", "show", unit,
                    "--property=CPUUsageNSec", "--property=MemoryCurrent",
                ]).splitlines()
                if "=" in line
            )
            try:
                cpu_ns = int(properties.get("CPUUsageNSec", "0"))
            except ValueError:
                cpu_ns = 0
            try:
                memory = int(properties.get("MemoryCurrent", "0"))
            except ValueError:
                memory = 0
            previous = self._previous_cpu.get(label)
            cpu_percent = None
            if previous and now > previous[1] and cpu_ns >= previous[0]:
                cpu_percent = (cpu_ns - previous[0]) / ((now - previous[1]) * 1e9) * 100
            self._previous_cpu[label] = (cpu_ns, now)
            sample[f"{label}_cpu_percent"] = cpu_percent
            sample[f"{label}_memory_mib"] = memory / 1024 / 1024 if memory else None
        return sample

    async def run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(await asyncio.to_thread(self._sample_sync))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def resource_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted({key for sample in samples for key in sample if key != "timestamp"})
    result: dict[str, Any] = {}
    for key in keys:
        values = [sample[key] for sample in samples if isinstance(sample.get(key), (int, float))]
        if not values or key.endswith("_name"):
            continue
        result[key] = {
            "mean": mean(values),
            "p95": percentile(values, 0.95),
            "max": max(values),
        }
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

