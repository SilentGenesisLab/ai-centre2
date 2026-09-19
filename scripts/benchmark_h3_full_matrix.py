from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx
import imageio_ffmpeg

from control_plane.config import get_settings
from control_plane.h3_scheduler import H3Scheduler
from control_plane.h3_store import H3Store


TERMINAL = {"succeeded", "failed", "cancelled"}
RESOLUTIONS = ("480p", "720p", "1080p")
DURATIONS = (5, 10, 15)
INPUT_MODES = ("text", "image", "video", "audio", "multimodal")
CONCURRENCIES = (1, 3, 6, 12)
BENCHMARK_PRIORITY = 1
MAX_IN_FLIGHT = 3
ASPECT_RATIOS = ("9:16", "16:9", "1:1", "4:3", "3:4")
ROUND_ORDERS = (
    (1, 3, 6, 12),
    (3, 6, 12, 1),
    (6, 12, 1, 3),
    (12, 1, 3, 6),
)
THEMES = (
    "portrait", "full_body_action", "product", "food", "beauty", "animal",
    "nature", "city", "architecture", "science_fiction", "macro", "documentary",
)
THEME_PACK_INDEX = {
    "portrait": 1, "full_body_action": 1, "beauty": 1, "documentary": 1,
    "product": 2, "food": 2, "macro": 2,
    "animal": 3, "nature": 3,
    "city": 4, "architecture": 4, "science_fiction": 4,
}
THEME_PROMPTS: dict[str, dict[str, str]] = {
    "portrait": {
        "zh": "一位年轻女性在窗边自然回头，神态放松，柔和晨光勾勒面部轮廓，镜头缓慢推近，写实电影质感",
        "en": "A young woman turns naturally beside a window with a relaxed expression, soft morning light shaping her face, slow camera push-in, realistic cinematic detail",
    },
    "full_body_action": {
        "zh": "一名舞者完成连贯的转身和抬手动作，衣摆随动作自然摆动，稳定全身镜头，舞台光影真实",
        "en": "A dancer performs a coherent turn and arm movement, fabric reacting naturally, stable full-body framing, realistic stage lighting",
    },
    "product": {
        "zh": "一款高端智能手表放在深色展台上，金属边缘出现流动高光，镜头环绕产品，商业广告质感",
        "en": "A premium smart watch rests on a dark pedestal, moving highlights trace the metal edge, camera orbit around the product, polished commercial style",
    },
    "food": {
        "zh": "热气从刚出炉的面包上缓慢升起，厨师切开酥脆表皮，暖色厨房背景，食物广告近景",
        "en": "Steam rises slowly from freshly baked bread as a chef cuts through the crisp crust, warm kitchen background, appetizing food commercial close-up",
    },
    "beauty": {
        "zh": "透明护肤精华瓶立在浅蓝水面，水波和微小水珠环绕瓶身，镜头平稳推进，干净高级的美妆广告",
        "en": "A transparent skincare serum bottle stands on pale blue water, ripples and tiny droplets surround it, smooth camera move, clean premium beauty commercial",
    },
    "animal": {
        "zh": "一只金毛犬在草地上迎着夕阳奔跑，毛发和草叶运动自然，低机位跟拍，温暖真实",
        "en": "A golden retriever runs across grass toward sunset, fur and blades of grass moving naturally, low tracking shot, warm realistic atmosphere",
    },
    "nature": {
        "zh": "清晨云雾穿过层叠山谷，阳光逐渐照亮森林，航拍镜头缓慢向前，宏大自然纪录片风格",
        "en": "Morning mist moves through layered valleys as sunlight gradually illuminates the forest, slow aerial advance, grand nature documentary style",
    },
    "city": {
        "zh": "雨后的现代城市街道倒映霓虹灯，行人和车辆自然移动，镜头沿街平稳前进，写实夜景",
        "en": "A modern city street after rain reflects neon lights, pedestrians and traffic move naturally, steady forward camera motion, realistic night scene",
    },
    "architecture": {
        "zh": "阳光穿过现代博物馆的几何结构，在墙面形成缓慢移动的光影，广角镜头横移，建筑摄影风格",
        "en": "Sunlight passes through the geometric structure of a modern museum, shadows move slowly across the walls, wide lateral camera move, architectural film style",
    },
    "science_fiction": {
        "zh": "未来空间站悬浮在蓝色星球上方，机械结构灯光依次点亮，镜头从远景缓慢接近，真实科幻电影画面",
        "en": "A future space station floats above a blue planet, mechanical lights activate in sequence, camera slowly approaches from a wide view, realistic science-fiction cinema",
    },
    "macro": {
        "zh": "微距镜头观察一滴水沿绿色叶片滑落，叶脉和水珠反射清晰，浅景深，自然光柔和变化",
        "en": "A macro shot follows a water droplet sliding across a green leaf, crisp veins and reflections, shallow depth of field, gentle natural light changes",
    },
    "documentary": {
        "zh": "工匠在安静工作室中专注打磨木制器物，手部动作准确连贯，环境光真实，纪录片式固定镜头",
        "en": "An artisan carefully polishes a wooden object in a quiet workshop, precise coherent hand movement, natural ambient light, observational documentary framing",
    },
}
REFERENCE_PROMPTS = (
    "一位穿蓝色外套的人在明亮工作室里自然转身并看向镜头，稳定全身构图，动作连贯，写实电影光线",
    "一款银色智能手表陈列在深色旋转展台上，镜头缓慢环绕，金属反光真实，高端产品广告",
    "云雾覆盖的绿色山谷在日出时逐渐变亮，航拍镜头平稳向前，自然纪录片质感",
    "雨后的现代城市街道闪烁蓝色和金色灯光，镜头平稳前进，车辆和行人自然运动",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_seed(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def public_url(value: str | None) -> str | None:
    if not value:
        return value
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def reference_count(payload: dict[str, Any], plural: str, *legacy: str) -> int:
    values = payload.get(plural)
    if isinstance(values, list):
        return len(values)
    return int(any(payload.get(key) for key in legacy))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def download(url: str, target: Path, *, timeout_seconds: float = 900) -> None:
    temporary = target.with_suffix(target.suffix + ".part")
    with httpx.stream(
        "GET", url, timeout=httpx.Timeout(timeout_seconds, connect=30), follow_redirects=True,
        trust_env=False,
    ) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                output.write(chunk)
    os.replace(temporary, target)


def upload(settings: Any, path: Path, stage: str, external_ref: str) -> str:
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
    }.get(path.suffix.lower(), "application/octet-stream")
    with path.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data={
                "external_ref": external_ref,
                "run_id": "", "campaign_id": "", "project_id": "",
                "stage": stage, "actor": "h3-full-benchmark",
            },
            files={"file": (path.name, stream, mime)},
            timeout=httpx.Timeout(900, connect=30),
        )
    response.raise_for_status()
    uri = str(response.json()["uri"])
    if not uri.startswith("https://"):
        raise RuntimeError("upload did not return a public HTTPS URL")
    return uri


class Benchmark:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.settings = get_settings()
        self.store = H3Store(self.settings.h3_db_path)
        self.scheduler = H3Scheduler(self.settings, self.store)
        self.run_dir = args.run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "state.json"
        self.manifest_path = self.run_dir / "manifest.json"
        self.events_path = self.run_dir / "job-events.jsonl"
        self.resources_path = self.run_dir / "resource-samples.jsonl"
        self.state: dict[str, Any] = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.is_file() else {
                "run_id": args.run_id,
                "created_at": utc_now(),
                "base_url": args.base_url,
                "assets": {},
                "previews": {},
                "cases": {},
                "status": "created",
            }
        )
        if self.state["run_id"] != args.run_id:
            raise RuntimeError("run directory belongs to a different run_id")
        self.client = httpx.Client(
            base_url=args.base_url,
            headers={"Authorization": f"Bearer {self.settings.service_token}"},
            timeout=httpx.Timeout(60, connect=20),
            trust_env=False,
        )
        self.stop_sampler = threading.Event()
        self.sampler_thread: threading.Thread | None = None
        self.deadline = time.monotonic() + args.deadline_hours * 3600

    def save(self) -> None:
        atomic_json(self.state_path, self.state)

    def start_sampler(self) -> None:
        if self.sampler_thread:
            return
        self.sampler_thread = threading.Thread(target=self._sample_loop, daemon=True)
        self.sampler_thread.start()

    def stop_sampling(self) -> None:
        self.stop_sampler.set()
        if self.sampler_thread:
            self.sampler_thread.join(timeout=10)

    def _sample_loop(self) -> None:
        previous_cpu: tuple[int, int] | None = None
        while not self.stop_sampler.is_set():
            started = time.monotonic()
            sample: dict[str, Any] = {"sampled_at": utc_now()}
            try:
                load = Path("/proc/loadavg").read_text(encoding="ascii").split()[:3]
                cpu_parts = [
                    int(value) for value in Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
                ]
                cpu_total = sum(cpu_parts)
                cpu_idle = cpu_parts[3] + (cpu_parts[4] if len(cpu_parts) > 4 else 0)
                cpu_percent = None
                if previous_cpu:
                    total_delta = cpu_total - previous_cpu[0]
                    idle_delta = cpu_idle - previous_cpu[1]
                    if total_delta > 0:
                        cpu_percent = max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100))
                previous_cpu = (cpu_total, cpu_idle)
                memory = {}
                for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                    key, raw = line.split(":", 1)
                    if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                        memory[key] = int(raw.strip().split()[0]) * 1024
                usage = shutil.disk_usage(self.run_dir)
                network_received = 0
                network_transmitted = 0
                for line in Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]:
                    _, raw = line.split(":", 1)
                    fields = raw.split()
                    network_received += int(fields[0])
                    network_transmitted += int(fields[8])
                disk_read = 0
                disk_written = 0
                for line in Path("/proc/diskstats").read_text(encoding="ascii").splitlines():
                    fields = line.split()
                    device = fields[2]
                    if not (device.startswith("nvme") and "p" not in device[5:]) and not device.startswith("sd"):
                        continue
                    disk_read += int(fields[5]) * 512
                    disk_written += int(fields[9]) * 512
                sample["controller"] = {
                    "load_1m": float(load[0]), "load_5m": float(load[1]), "load_15m": float(load[2]),
                    "cpu_utilization_percent": round(cpu_percent, 3) if cpu_percent is not None else None,
                    "memory_total_bytes": memory.get("MemTotal"),
                    "memory_used_bytes": (
                        memory.get("MemTotal", 0) - memory.get("MemAvailable", 0)
                    ),
                    "swap_used_bytes": memory.get("SwapTotal", 0) - memory.get("SwapFree", 0),
                    "disk_used_bytes": usage.used, "disk_free_bytes": usage.free,
                    "disk_read_bytes_total": disk_read, "disk_written_bytes_total": disk_written,
                    "network_received_bytes_total": network_received,
                    "network_transmitted_bytes_total": network_transmitted,
                }
            except Exception as exc:
                sample["controller_error"] = type(exc).__name__
            try:
                sample["workers"] = [
                    {
                        "name": item.get("name"), "status": item.get("status"),
                        "enabled": item.get("enabled"), "compatible": item.get("compatible"),
                        "queue_running": item.get("queue_running"),
                        "queue_pending": item.get("queue_pending"),
                        "current_job": bool(item.get("current_job_id")),
                        "vram_total_bytes": item.get("vram_total_bytes"),
                        "vram_free_bytes": item.get("vram_free_bytes"),
                    }
                    for item in self.store.list_workers(public=False)
                ]
            except Exception as exc:
                sample["workers_error"] = type(exc).__name__
            try:
                completed = subprocess.run(
                    [
                        "nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
                        "temperature.gpu,power.draw", "--format=csv,noheader,nounits",
                    ], capture_output=True, text=True, timeout=5, check=False,
                )
                if completed.returncode == 0:
                    sample["local_gpus"] = [
                        {
                            "index": int(parts[0]), "name": parts[1],
                            "utilization_percent": float(parts[2]),
                            "memory_used_mib": float(parts[3]), "memory_total_mib": float(parts[4]),
                            "temperature_c": float(parts[5]), "power_w": float(parts[6]),
                        }
                        for line in completed.stdout.splitlines() if line.strip()
                        for parts in ([value.strip() for value in line.split(",")],)
                    ]
            except Exception as exc:
                sample["local_gpu_error"] = type(exc).__name__
            append_jsonl(self.resources_path, sample)
            remaining = self.args.sample_seconds - (time.monotonic() - started)
            self.stop_sampler.wait(max(0.1, remaining))

    def _active_jobs(self) -> list[dict[str, Any]]:
        return [
            job for job in self.store.list_jobs(limit=5000)["items"]
            if job.get("status") in {"queued", "running", "cancel_requested"}
        ]

    def preflight(self) -> None:
        if any(item.get("job_id") for section in ("previews", "cases") for item in self.state[section].values()):
            return
        consecutive = 0
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            self.scheduler.probe_all()
            workers = [
                item for item in self.store.list_workers(public=False)
                if item.get("enabled") and item.get("compatible") is not False
                and item.get("status") in {"online", "busy"}
            ]
            active = self._active_jobs()
            ready = len(workers) >= 3 and not active and all(
                int(item.get("queue_running") or 0) == 0 and int(item.get("queue_pending") or 0) == 0
                for item in workers
            )
            append_jsonl(self.events_path, {
                "event": "preflight", "at": utc_now(), "ready": ready,
                "worker_names": [item.get("name") for item in workers],
                "active_job_count": len(active),
            })
            consecutive = consecutive + 1 if ready else 0
            if consecutive >= 2:
                self.state["preflight_completed_at"] = utc_now()
                self.save()
                return
            time.sleep(10)
        raise RuntimeError("three healthy idle H3 workers were not available for two checks")

    def recover_job_id(self, external_ref: str) -> str | None:
        for job in self.store.list_jobs(limit=5000)["items"]:
            if job.get("external_ref") == external_ref:
                return str(job["job_id"])
        return None

    def submit(self, record: dict[str, Any]) -> None:
        if record.get("job_id") or record.get("status") == "submit_failed":
            return
        recovered = self.recover_job_id(record["payload"]["external_ref"])
        if recovered:
            record["job_id"] = recovered
            record["status"] = "submitted"
            record["recovered"] = True
            self.save()
            return
        record["payload"]["priority"] = BENCHMARK_PRIORITY
        started = time.perf_counter()
        response = self.client.post("/v1/video-generations/minimax-h3/jobs", json=record["payload"])
        latency = round((time.perf_counter() - started) * 1000, 3)
        record["submit_http"] = response.status_code
        record["submit_latency_ms"] = latency
        record["submitted_at"] = utc_now()
        if response.status_code == 202:
            record["job_id"] = str(response.json()["job_id"])
            record["status"] = "submitted"
        else:
            record["status"] = "submit_failed"
            try:
                record["submit_error"] = response.json().get("detail")
            except Exception:
                record["submit_error"] = response.text[:500]
        append_jsonl(self.events_path, {
            "event": "submitted", "at": utc_now(), "case_id": record["case_id"],
            "job_id": record.get("job_id"), "http": response.status_code,
            "latency_ms": latency,
        })
        self.save()

    def poll(self, record: dict[str, Any]) -> bool:
        if record.get("status") in TERMINAL | {"submit_failed", "benchmark_timeout"}:
            return True
        job_id = record.get("job_id")
        if not job_id:
            return False
        response = self.client.get(f"/v1/video-generations/minimax-h3/jobs/{job_id}")
        if response.status_code != 200:
            append_jsonl(self.events_path, {
                "event": "poll_error", "at": utc_now(), "case_id": record["case_id"],
                "job_id": job_id, "http": response.status_code,
            })
            return False
        job = response.json()
        signature = f"{job.get('status')}:{job.get('stage')}:{job.get('progress')}"
        if record.get("last_signature") != signature:
            record["last_signature"] = signature
            record["status"] = str(job.get("status") or "unknown")
            append_jsonl(self.events_path, {
                "event": "state", "at": utc_now(), "case_id": record["case_id"],
                "job_id": job_id, "status": job.get("status"), "stage": job.get("stage"),
                "progress": job.get("progress"),
            })
            self.save()
        if job.get("status") in TERMINAL:
            record["status"] = job["status"]
            record["final"] = job
            record["finished_observed_at"] = utc_now()
            self.save()
            return True
        record["status"] = str(job.get("status") or "unknown")
        return False

    def cancel_unfinished(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            if record.get("job_id") and record.get("status") not in TERMINAL:
                try:
                    self.client.post(
                        f"/v1/video-generations/minimax-h3/jobs/{record['job_id']}/cancel"
                    )
                except Exception:
                    pass
                record["status"] = "benchmark_timeout"
        self.save()

    def run_window(self, records: list[dict[str, Any]], concurrency: int, label: str) -> None:
        pending = [item for item in records if item.get("status") not in TERMINAL | {"submit_failed"}]
        effective_concurrency = min(concurrency, MAX_IN_FLIGHT)
        append_jsonl(self.events_path, {
            "event": "window_started", "at": utc_now(), "label": label,
            "concurrency": concurrency, "effective_concurrency": effective_concurrency,
            "case_count": len(records), "pending_count": len(pending),
        })
        while pending:
            if time.monotonic() >= self.deadline:
                self.cancel_unfinished(pending)
                raise TimeoutError("benchmark deadline reached")
            active = [
                item for item in pending
                if item.get("job_id") and item.get("status") not in TERMINAL | {"submit_failed"}
            ]
            open_slots = max(0, effective_concurrency - len(active))
            for record in [item for item in pending if not item.get("job_id")][:open_slots]:
                self.submit(record)
            completed = []
            for record in pending:
                if record.get("status") == "submit_failed" or self.poll(record):
                    completed.append(record)
            if completed:
                pending = [item for item in pending if item not in completed]
                succeeded = sum(item.get("status") == "succeeded" for item in records)
                print(
                    f"WINDOW {label} completed={len(records)-len(pending)}/{len(records)} "
                    f"succeeded={succeeded}", flush=True,
                )
            if pending:
                time.sleep(self.args.poll_seconds)
        append_jsonl(self.events_path, {
            "event": "window_finished", "at": utc_now(), "label": label,
            "concurrency": concurrency, "effective_concurrency": effective_concurrency,
        })

    def prepare_assets(self) -> None:
        if self.state["assets"].get("ready"):
            return
        previews = self.state["previews"]
        for index, prompt in enumerate(REFERENCE_PROMPTS, start=1):
            case_id = f"reference-{index}"
            previews.setdefault(case_id, {
                "case_id": case_id,
                "kind": "reference_preparation",
                "payload": {
                    "segment_mode": "single", "prompts": [prompt],
                    "duration_seconds": 15, "resolution": "480p", "aspect_ratio": "16:9",
                    "priority": BENCHMARK_PRIORITY,
                    "seed": stable_seed(f"{self.args.run_id}:{case_id}"),
                    "external_ref": f"{self.args.run_id}-{case_id}",
                    "metadata": {"purpose": "h3-full-benchmark-reference", "case_id": case_id},
                },
                "status": "created",
            })
        self.save()

        self.run_window(list(previews.values()), 3, "reference-preparation")
        if any(item.get("status") != "succeeded" for item in previews.values()):
            raise RuntimeError("one or more reference preparation videos failed")

        asset_dir = self.run_dir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        packs: list[dict[str, Any]] = []
        frequencies = (220, 277, 330, 392)
        for index in range(1, 5):
            record = previews[f"reference-{index}"]
            result_url = str(record["final"]["result_url"])
            video = asset_dir / f"reference-{index}.mp4"
            image = asset_dir / f"reference-{index}.png"
            audio = asset_dir / f"reference-{index}.wav"
            if not video.is_file():
                download(result_url, video)
            if not image.is_file():
                subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "7.5",
                    "-i", str(video), "-frames:v", "1", str(image),
                ], check=True)
            if not audio.is_file():
                subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={frequencies[index-1]}:sample_rate=48000:duration=15",
                    "-af", "volume=0.12,afade=t=in:st=0:d=0.5,afade=t=out:st=14:d=1",
                    "-c:a", "pcm_s16le", str(audio),
                ], check=True)
            image_url = upload(
                self.settings, image, "validation.h3_full.reference_image",
                f"{self.args.run_id}-reference-image-{index}",
            )
            audio_url = upload(
                self.settings, audio, "validation.h3_full.reference_audio",
                f"{self.args.run_id}-reference-audio-{index}",
            )
            packs.append({
                "index": index, "video_url": result_url, "image_url": image_url,
                "audio_url": audio_url,
                "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
            })

        concat_list = asset_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{(asset_dir / f'reference-{index}.mp4').as_posix()}'" for index in (1, 2)),
            encoding="utf-8",
        )
        two_part_urls = {}
        for seconds in (24, 30):
            target = asset_dir / f"reference-two-part-{seconds}s.mp4"
            if not target.is_file():
                subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(concat_list), "-t", str(seconds), "-c", "copy", str(target),
                ], check=True)
            two_part_urls[str(seconds)] = upload(
                self.settings, target, "validation.h3_full.reference_video_two_part",
                f"{self.args.run_id}-reference-two-part-{seconds}s",
            )
        self.state["assets"] = {
            "ready": True, "prepared_at": utc_now(), "packs": packs,
            "two_part_video_urls": two_part_urls,
        }
        self.save()

    def ensure_two_part_continuity_assets(self) -> None:
        if self.state["assets"].get("two_part_continuity_ready"):
            return
        asset_dir = self.run_dir / "assets"
        source = asset_dir / "reference-1.mp4"
        if not source.is_file():
            raise RuntimeError("person reference video is missing")
        concat_list = asset_dir / "concat-continuity.txt"
        concat_list.write_text(
            f"file '{source.as_posix()}'\nfile '{source.as_posix()}'", encoding="utf-8"
        )
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        urls = {}
        for seconds in (24, 30):
            target = asset_dir / f"reference-two-part-continuity-{seconds}s.mp4"
            if not target.is_file():
                subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(concat_list), "-t", str(seconds),
                    "-c", "copy", str(target),
                ], check=True)
            urls[str(seconds)] = upload(
                self.settings, target, "validation.h3_full.reference_video_two_part_continuity",
                f"{self.args.run_id}-reference-two-part-continuity-{seconds}s",
            )
        self.state["assets"]["two_part_video_urls"] = urls
        self.state["assets"]["two_part_continuity_ready"] = True
        self.save()

    @staticmethod
    def reference_tags(input_mode: str, language: str) -> str:
        tags = {
            "text": "",
            "image": "<Picture 1> <Picture 2>",
            "video": "<Video 1> <Video 2>",
            "audio": "<Audio 1> <Audio 2>",
            "multimodal": "<Picture 1> <Picture 2> <Video 1> <Video 2> <Audio 1> <Audio 2>",
        }[input_mode]
        if not tags:
            return ""
        if language == "zh":
            return f"使用{tags}作为参考，保持参考主体和节奏自然一致。"
        return f"Use {tags} as references and preserve the referenced subject and rhythm naturally. "

    def build_single_cases(self) -> list[dict[str, Any]]:
        packs = self.state["assets"]["packs"]
        cases = self.state["cases"]
        base_index = 0
        for resolution in RESOLUTIONS:
            for duration in DURATIONS:
                for input_mode in INPUT_MODES:
                    for concurrency_index, concurrency in enumerate(CONCURRENCIES):
                        case_id = f"single-{resolution}-{duration}s-{input_mode}-c{concurrency}"
                        global_index = base_index * len(CONCURRENCIES) + concurrency_index
                        language = "zh" if global_index % 2 == 0 else "en"
                        theme_index = (base_index + 2 * concurrency_index) % len(THEMES)
                        theme = THEMES[theme_index]
                        aspect_ratio = ASPECT_RATIOS[global_index % len(ASPECT_RATIOS)]
                        desired_pack_index = THEME_PACK_INDEX[theme]
                        pack = packs[desired_pack_index - 1]
                        secondary_pack = packs[desired_pack_index % len(packs)]
                        prompt = self.reference_tags(input_mode, language) + THEME_PROMPTS[theme][language]
                        payload: dict[str, Any] = {
                            "segment_mode": "single", "prompts": [prompt],
                            "duration_seconds": duration, "resolution": resolution,
                            "aspect_ratio": aspect_ratio,
                            "reference_image_urls": [], "reference_video_urls": [],
                            "reference_audio_urls": [],
                            "priority": BENCHMARK_PRIORITY,
                            "seed": stable_seed(f"{self.args.run_id}:{case_id}"),
                            "external_ref": f"{self.args.run_id}-{case_id}",
                            "metadata": {
                                "purpose": "h3-full-benchmark", "case_id": case_id,
                                "input_mode": input_mode, "concurrency": concurrency,
                                "theme": theme, "language": language,
                            },
                        }
                        if input_mode in {"image", "multimodal"}:
                            payload["reference_image_urls"] = [pack["image_url"], secondary_pack["image_url"]]
                        if input_mode in {"video", "multimodal"}:
                            payload["reference_video_urls"] = [pack["video_url"], secondary_pack["video_url"]]
                        if input_mode in {"audio", "multimodal"}:
                            payload["reference_audio_urls"] = [pack["audio_url"], secondary_pack["audio_url"]]
                        desired_record = {
                            "case_id": case_id, "kind": "single", "resolution": resolution,
                            "duration_seconds": duration, "input_mode": input_mode,
                            "concurrency": concurrency, "aspect_ratio": aspect_ratio,
                            "theme": theme, "language": language, "payload": payload,
                            "reference_pack_index": desired_pack_index,
                            "reference_compatible": True,
                            "status": "created",
                        }
                        current = cases.get(case_id)
                        if current is None or not current.get("job_id"):
                            cases[case_id] = desired_record
                        else:
                            current_payload = current.get("payload", {})
                            actual_urls = {
                                value
                                for key in ("reference_image_urls", "reference_video_urls")
                                for value in current_payload.get(key, [])
                            }
                            actual_urls.update({
                                current_payload.get("reference_image_url"),
                                current_payload.get("reference_video_url"),
                            })
                            desired_urls = {pack.get("image_url"), pack.get("video_url")}
                            current["reference_pack_index"] = next((
                                int(candidate["index"]) for candidate in packs
                                if actual_urls & {candidate.get("image_url"), candidate.get("video_url")}
                            ), None)
                            current["reference_compatible"] = (
                                input_mode not in {"image", "video", "multimodal"}
                                or bool(actual_urls & desired_urls)
                            )
                    base_index += 1
        return [item for item in cases.values() if item["kind"] == "single"]

    def build_two_part_cases(self) -> list[dict[str, Any]]:
        cases = self.state["cases"]
        packs = self.state["assets"]["packs"]
        urls = self.state["assets"]["two_part_video_urls"]
        for resolution_index, resolution in enumerate(RESOLUTIONS):
            for total_seconds in (24, 30):
                split = total_seconds / 2
                for mode_index, input_mode in enumerate(("video", "multimodal")):
                    case_id = f"two-part-{resolution}-{total_seconds}s-{input_mode}"
                    language = "zh" if (resolution_index + mode_index + total_seconds) % 2 == 0 else "en"
                    aspect_ratio = ASPECT_RATIOS[(resolution_index * 4 + mode_index + total_seconds) % 5]
                    pack = packs[0]
                    secondary_pack = packs[1]
                    tag = self.reference_tags(input_mode, language)
                    prompts = [
                        tag + THEME_PROMPTS["full_body_action"][language],
                        tag + THEME_PROMPTS["documentary"][language],
                    ]
                    payload: dict[str, Any] = {
                        "reference_video_urls": [urls[str(total_seconds)], secondary_pack["video_url"]],
                        "reference_image_urls": [], "reference_audio_urls": [],
                        "segment_mode": "two_part", "prompts": prompts,
                        "split_seconds": split, "resolution": resolution,
                        "aspect_ratio": aspect_ratio,
                        "priority": BENCHMARK_PRIORITY,
                        "seed": stable_seed(f"{self.args.run_id}:{case_id}"),
                        "external_ref": f"{self.args.run_id}-{case_id}",
                        "metadata": {
                            "purpose": "h3-full-benchmark", "case_id": case_id,
                            "input_mode": input_mode, "concurrency": 3,
                            "theme": "two_part_continuity", "language": language,
                        },
                    }
                    if input_mode == "multimodal":
                        payload["reference_image_urls"] = [pack["image_url"], secondary_pack["image_url"]]
                        payload["reference_audio_urls"] = [pack["audio_url"], secondary_pack["audio_url"]]
                    desired_record = {
                        "case_id": case_id, "kind": "two_part", "resolution": resolution,
                        "duration_seconds": total_seconds, "input_mode": input_mode,
                        "concurrency": 3, "aspect_ratio": aspect_ratio,
                        "theme": "two_part_continuity", "language": language,
                        "reference_pack_index": 1, "reference_compatible": True,
                        "payload": payload, "status": "created",
                    }
                    current = cases.get(case_id)
                    if current is None or not current.get("job_id"):
                        cases[case_id] = desired_record
        return [item for item in cases.values() if item["kind"] == "two_part"]

    def write_manifest(self) -> None:
        records = list(self.state["cases"].values())
        manifest = {
            "run_id": self.args.run_id, "generated_at": utc_now(),
            "counts": {
                "total": len(records),
                "kind": Counter(item["kind"] for item in records),
                "resolution": Counter(item["resolution"] for item in records),
                "duration": Counter(str(item["duration_seconds"]) for item in records),
                "input_mode": Counter(item["input_mode"] for item in records),
                "concurrency": Counter(str(item["concurrency"]) for item in records),
                "aspect_ratio": Counter(item["aspect_ratio"] for item in records),
                "language": Counter(item["language"] for item in records),
            },
            "cases": [
                {
                    key: value for key, value in item.items()
                    if key not in {"status", "job_id", "final", "last_signature"}
                }
                for item in records
            ],
            "assets": {
                "packs": [
                    {key: public_url(value) if key.endswith("_url") else value for key, value in pack.items()}
                    for pack in self.state["assets"]["packs"]
                ],
                "two_part_video_urls": {
                    key: public_url(value)
                    for key, value in self.state["assets"]["two_part_video_urls"].items()
                },
            },
        }
        atomic_json(self.manifest_path, manifest)

    @staticmethod
    def split_four(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        sizes = (12, 11, 11, 11)
        chunks = []
        offset = 0
        for size in sizes:
            chunks.append(records[offset:offset + size])
            offset += size
        if offset != len(records):
            raise RuntimeError(f"expected 45 records, got {len(records)}")
        return chunks

    def run_matrix(self) -> None:
        single = self.build_single_cases()
        two_part = self.build_two_part_cases()
        self.save()
        self.write_manifest()
        if len(single) != 180 or len(two_part) != 12:
            raise RuntimeError(f"invalid matrix size: single={len(single)} two_part={len(two_part)}")
        by_concurrency = {
            concurrency: sorted(
                [item for item in single if item["concurrency"] == concurrency],
                key=lambda item: (
                    0 if item.get("job_id") else 1,
                    stable_seed(f"{self.args.run_id}:schedule:{item['case_id']}")
                ),
            )
            for concurrency in CONCURRENCIES
        }
        chunks = {key: self.split_four(value) for key, value in by_concurrency.items()}
        for round_index, order in enumerate(ROUND_ORDERS):
            for concurrency in order:
                self.run_window(
                    chunks[concurrency][round_index], concurrency,
                    f"single-round-{round_index + 1}-c{concurrency}",
                )
        self.run_window(sorted(two_part, key=lambda item: item["case_id"]), 3, "two-part-c3")

    def export_results(self) -> None:
        records = list(self.state["cases"].values())
        rows = []
        for item in records:
            final = item.get("final") or {}
            payload = item["payload"]
            created = final.get("created_at")
            finished = final.get("finished_at")
            wall = None
            if created and finished:
                wall = (
                    datetime.fromisoformat(finished) - datetime.fromisoformat(created)
                ).total_seconds()
            rows.append({
                "case_id": item["case_id"], "kind": item["kind"],
                "resolution": item["resolution"], "duration_seconds": item["duration_seconds"],
                "input_mode": item["input_mode"], "concurrency": item["concurrency"],
                "aspect_ratio": item["aspect_ratio"], "theme": item["theme"],
                "language": item["language"], "seed": payload["seed"],
                "reference_contract": "plural" if all(
                    key in payload for key in (
                        "reference_image_urls", "reference_video_urls", "reference_audio_urls",
                    )
                ) else "legacy",
                "reference_image_count": reference_count(
                    payload, "reference_image_urls", "reference_image_url", "identity_image_url",
                ),
                "reference_video_count": reference_count(
                    payload, "reference_video_urls", "reference_video_url",
                ),
                "reference_audio_count": reference_count(
                    payload, "reference_audio_urls", "reference_audio_url",
                ),
                "job_id": item.get("job_id"), "status": item.get("status"),
                "submit_http": item.get("submit_http"),
                "submit_latency_ms": item.get("submit_latency_ms"),
                "attempt_count": final.get("attempt_count"), "stage": final.get("stage"),
                "wall_seconds": round(wall, 3) if wall is not None else None,
                "elapsed_seconds": final.get("elapsed_seconds"),
                "rtf": round(wall / item["duration_seconds"], 4) if wall is not None else None,
                "result_url": public_url(final.get("result_url")),
                "error": final.get("error"),
                "prompt": " || ".join(payload["prompts"]),
            })
        atomic_json(self.run_dir / "results.json", rows)
        with (self.run_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.state["status"] = "generation_completed"
        self.state["generation_completed_at"] = utc_now()
        self.save()

    def run(self) -> None:
        self.start_sampler()
        try:
            self.preflight()
            self.state["status"] = "preparing_assets"
            self.save()
            self.prepare_assets()
            self.ensure_two_part_continuity_assets()
            self.state["status"] = "running_matrix"
            self.save()
            self.run_matrix()
            self.export_results()
        except Exception as exc:
            self.state["status"] = "interrupted"
            self.state["last_error"] = f"{type(exc).__name__}: {exc}"
            self.state["interrupted_at"] = utc_now()
            self.save()
            raise
        finally:
            self.stop_sampling()
            self.client.close()


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the resumable MiniMax H3 192-case benchmark")
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--run-id", default="h3-full-20260902")
    parser.add_argument(
        "--run-dir", type=Path,
        default=Path("runtime_validation/h3-full-benchmark-20260902"),
    )
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--sample-seconds", type=float, default=2)
    parser.add_argument("--deadline-hours", type=float, default=24)
    args = parser.parse_args()
    benchmark = Benchmark(args)
    benchmark.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
