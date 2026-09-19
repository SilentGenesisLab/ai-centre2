from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


MACHINE_PROFILES = {
    "4090_24g": {"device_name": "4090", "gpu_memory_min": 24000, "gpu_memory_max": 30000},
    "4090_48g": {"device_name": "4090-48G", "gpu_memory_min": 48000, "gpu_memory_max": 60000},
    "5090_32g": {"device_name": "5090", "gpu_memory_min": 32000, "gpu_memory_max": 40000},
}

H3_IMAGE = "harbor.suanleme.cn/public-hub/minimax-h3-comfyui:minimax-h3-20260820164830432"
H3_ENV = 'CMD=python main.py --listen 0.0.0.0 --enable-cors-header "*"'
PROVIDER_MODES = {"spot", "deployment"}


class SuanliError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SuanliResource:
    mark: str
    resource: dict[str, Any]
    region_name: str
    inventory: int
    price: int


class SuanliClient:
    def __init__(self, token: str, base_url: str = "https://openapi.suanli.cn", timeout: float = 30) -> None:
        if not token:
            raise SuanliError("SUANLI_TOKEN is not configured")
        self.token = token
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"token": self.token, "timestamp": str(int(time.time() * 1000)), "version": "1.0.0"}

    def _call(self, method: str, path: str, *, params=None, json=None) -> Any:
        response = self.client.request(method, path, params=params, json=json, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0000":
            code = str(payload.get("code") or "UNKNOWN")
            raise SuanliError(
                f"{code}: {payload.get('message') or 'Suanli API failed'}",
                code=code,
            )
        return payload.get("data")

    def resource(self, machine_type: str, provider_mode: str = "deployment") -> SuanliResource:
        profile = MACHINE_PROFILES.get(machine_type)
        if not profile:
            raise SuanliError(f"unsupported machine type: {machine_type}")
        if provider_mode not in PROVIDER_MODES:
            raise SuanliError(f"unsupported provider mode: {provider_mode}")
        task_type = "Job" if provider_mode == "spot" else "Deployment"
        data = self._call(
            "GET", "/api/deployment/resource/search",
            params={"task_type": task_type, "device_type": "GpuDevice"},
        )
        candidates: list[SuanliResource] = []
        for item in (data or {}).get("results", []):
            if item.get("device_name") != profile["device_name"] or int(item.get("gpu_count") or 0) != 1:
                continue
            memory = int(item.get("gpu_memory") or 0)
            if not profile["gpu_memory_min"] <= memory <= profile["gpu_memory_max"]:
                continue
            for region in item.get("regions") or []:
                if int(region.get("inventory") or 0) <= 0:
                    continue
                mark = region.get("mark") or {}
                candidates.append(SuanliResource(
                    mark=str(mark.get("mark")), resource=dict(mark.get("resource") or {}),
                    region_name=str(region.get("region_name") or ""), inventory=int(region.get("inventory") or 0),
                    price=int(region.get("discount_price") or region.get("price") or 0),
                ))
        if not candidates:
            raise SuanliError(f"no available inventory for {machine_type}")
        return sorted(candidates, key=lambda item: (-item.inventory, item.price))[0]

    def create_h3_worker(
        self,
        name: str,
        machine_type: str,
        *,
        provider_mode: str = "spot",
        spot_estimated_exec_seconds: int = 82800,
    ) -> int:
        selected = self.resource(machine_type, provider_mode)
        resources = [{
            "mark": selected.mark,
            "resource": selected.resource,
            "region_name": selected.region_name,
        }]
        if provider_mode == "spot":
            estimated = max(1, min(86400, int(spot_estimated_exec_seconds)))
            data = self._call("POST", "/api/task/job/create", json={
                "task_name": name,
                "sub_type": "Spot",
                "resources": resources,
                "points": 1,
                "job_support": {
                    "estimated_exec_sec": estimated,
                    "timeout_sec": estimated + 3600,
                    "parallelism": 1,
                    "mod_param": {
                        "default": {"backoff_limit": 3, "restart_policy": "OnFailure"},
                    },
                },
                "services": [{
                    "service_name": "h3-comfyui",
                    "service_image": H3_IMAGE,
                    "resource_weight": {"cpu_weight": 1, "mem_weight": 1, "gpu_weight": 1},
                    "remote_ports": [{"service_port": 8188}, {"service_port": 3000}],
                    "env": [{"name": "CMD", "value": H3_ENV.removeprefix("CMD=")}],
                    "start_script": {
                        "command": ["sh", "-lc"],
                        "args": [H3_ENV.removeprefix("CMD=")],
                    },
                }],
            })
        else:
            data = self._call("POST", "/api/deployment/task/create", json={
                "task_type": "Deployment", "task_name": name, "points": 1,
                "resources": resources,
                "services": [{
                    "service_name": "h3-comfyui", "service_image": H3_IMAGE, "env": H3_ENV,
                    "remote_ports": [{"service_port": 8188}, {"service_port": 3000}],
                    "start_script_v2": {"command": None, "args": []},
                }],
            })
        return int(data["task_id"])

    def detail(self, task_id: int, *, provider_mode: str = "deployment") -> dict[str, Any]:
        path = "/api/task/job/detail" if provider_mode == "spot" else "/api/deployment/task/detail"
        return dict(self._call("GET", path, params={"task_id": task_id}) or {})

    def stop(self, task_id: int, *, provider_mode: str = "deployment") -> None:
        path = "/api/task/job/stop" if provider_mode == "spot" else "/api/deployment/task/stop"
        self._call("POST", path, json={"task_id": task_id})

    @staticmethod
    def worker_url(detail: dict[str, Any]) -> str | None:
        for service in detail.get("services") or []:
            for port in service.get("remote_ports") or []:
                if int(port.get("service_port") or 0) == 8188 and port.get("url"):
                    return str(port["url"]).rstrip("/")
        return None
