from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import redis

from .config import Settings
from .h3_store import H3Store
from .h3_workflow import REQUIRED_MODELS, REQUIRED_NODES


def _queue_counts(payload: Any) -> tuple[int, int]:
    if isinstance(payload, dict):
        running = payload.get("queue_running", [])
        pending = payload.get("queue_pending", [])
        return len(running) if isinstance(running, list) else 0, len(pending) if isinstance(pending, list) else 0
    if isinstance(payload, list) and len(payload) >= 2:
        return len(payload[0] or []), len(payload[1] or [])
    return 0, 0


def _gpu_stats(payload: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        return None, None, None
    device = devices[0] if isinstance(devices[0], dict) else {}
    return (
        str(device.get("name")) if device.get("name") else None,
        int(device["vram_total"]) if isinstance(device.get("vram_total"), int | float) else None,
        int(device["vram_free"]) if isinstance(device.get("vram_free"), int | float) else None,
    )


def probe_worker(base_url: str, timeout_seconds: float, *, check_compatibility: bool = True) -> dict[str, Any]:
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            stats_response = client.get("/system_stats")
            stats_response.raise_for_status()
            stats = stats_response.json()
            queue_response = client.get("/queue")
            queue_response.raise_for_status()
            running, pending = _queue_counts(queue_response.json())
            compatible = True
            missing: list[str] = []
            if check_compatibility:
                object_response = client.get("/object_info")
                object_response.raise_for_status()
                object_info = object_response.json()
                missing.extend(node for node in REQUIRED_NODES if node not in object_info)
                serialized = json.dumps(object_info, ensure_ascii=False)
                missing.extend(model for model in REQUIRED_MODELS if model not in serialized)
                compatible = not missing
            gpu_name, vram_total, vram_free = _gpu_stats(stats)
            return {
                "ok": True,
                "compatible": compatible,
                "queue_running": running,
                "queue_pending": pending,
                "gpu_name": gpu_name,
                "vram_total_bytes": vram_total,
                "vram_free_bytes": vram_free,
                "error": f"missing H3 components: {', '.join(missing)}" if missing else None,
            }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "compatible": True,
            "queue_running": 0,
            "queue_pending": 0,
            "error": f"{type(exc).__name__}: worker health check failed",
        }


class H3Scheduler:
    def __init__(self, settings: Settings, store: H3Store) -> None:
        self.settings = settings
        self.store = store
        self.redis = redis.Redis.from_url(settings.redis_result_url, decode_responses=True)

    @staticmethod
    def _lease_key(worker_id: str) -> str:
        return f"ai-centre2:h3:worker:{worker_id}:lease"

    def probe_all(self, *, check_compatibility: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for public_worker in self.store.list_workers(public=False):
            result = probe_worker(
                str(public_worker["base_url"]),
                self.settings.h3_health_timeout_seconds,
                check_compatibility=check_compatibility or public_worker["status"] in {"unknown", "incompatible"},
            )
            results.append(self.store.record_probe(public_worker["id"], **result))
        return results

    def acquire(self, job_id: str, attempt_number: int) -> tuple[dict[str, Any], str] | None:
        if not self.store.is_next_dispatch_job(job_id):
            return None
        for worker in self.store.schedulable_workers():
            lease_version = uuid4().hex
            lease_value = f"{job_id}:{attempt_number}:{lease_version}"
            acquired = self.redis.set(
                self._lease_key(str(worker["id"])),
                lease_value,
                nx=True,
                ex=self.settings.h3_lease_seconds,
            )
            if acquired:
                worker["lease_value"] = lease_value
                return worker, lease_version
        return None

    def renew(self, worker_id: str, lease_value: str) -> bool:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """
        return bool(self.redis.eval(
            script, 1, self._lease_key(worker_id), lease_value, self.settings.h3_lease_seconds
        ))

    def release(self, worker_id: str, lease_value: str) -> None:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        self.redis.eval(script, 1, self._lease_key(worker_id), lease_value)
