from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MANAGED_SERVICES: dict[int, tuple[str, ...]] = {
    0: (
        "ai-centre-face-worker-gpu0.service",
        "ai-centre-ocr-worker@0.service",
    ),
    1: (
        "ai-centre-face-worker-gpu1.service",
        "ai-centre-ocr-worker@1.service",
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], CommandResult]


def _run(command: list[str]) -> CommandResult:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


class GpuController:
    def __init__(self, state_path: Path, runner: Runner = _run) -> None:
        self.state_path = state_path
        self.runner = runner

    @staticmethod
    def validate_gpu(gpu_id: int) -> None:
        if gpu_id not in MANAGED_SERVICES:
            raise ValueError(f"unsupported gpu_id: {gpu_id}")

    def service_state(self, service: str) -> dict[str, str | bool]:
        result = self.runner(
            [
                "systemctl",
                "--user",
                "show",
                service,
                "--property=ActiveState,SubState,UnitFileState",
            ]
        )
        values: dict[str, str | bool] = {
            "name": service,
            "query_ok": result.returncode == 0,
        }
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if result.stderr:
            values["error"] = result.stderr
        return values

    def gpu_state(self, gpu_id: int) -> dict[str, object]:
        self.validate_gpu(gpu_id)
        stored = self._read_state()
        return {
            "gpu_id": gpu_id,
            "enabled": stored.get(str(gpu_id), {}).get("enabled", True),
            "services": [
                self.service_state(service) for service in MANAGED_SERVICES[gpu_id]
            ],
        }

    def all_states(self) -> list[dict[str, object]]:
        return [self.gpu_state(gpu_id) for gpu_id in sorted(MANAGED_SERVICES)]

    def drain(self, gpu_id: int, disable: bool = False) -> dict[str, object]:
        self.validate_gpu(gpu_id)
        actions: list[dict[str, object]] = []
        for service in MANAGED_SERVICES[gpu_id]:
            command = ["systemctl", "--user", "disable" if disable else "stop"]
            if disable:
                command.append("--now")
            command.append(service)
            result = self.runner(command)
            actions.append(
                {
                    "service": service,
                    "action": "disable" if disable else "stop",
                    "ok": result.returncode == 0,
                    "error": result.stderr or None,
                }
            )
        succeeded = all(bool(action["ok"]) for action in actions)
        if succeeded:
            self._set_enabled(gpu_id, False)
        return {
            "gpu_id": gpu_id,
            "status": "disabled" if disable and succeeded else "drained" if succeeded else "failed",
            "actions": actions,
        }

    def enable(self, gpu_id: int) -> dict[str, object]:
        self.validate_gpu(gpu_id)
        actions: list[dict[str, object]] = []
        for service in MANAGED_SERVICES[gpu_id]:
            result = self.runner(["systemctl", "--user", "enable", "--now", service])
            actions.append(
                {
                    "service": service,
                    "action": "enable",
                    "ok": result.returncode == 0,
                    "error": result.stderr or None,
                }
            )
        succeeded = all(bool(action["ok"]) for action in actions)
        if succeeded:
            self._set_enabled(gpu_id, True)
        return {
            "gpu_id": gpu_id,
            "status": "enabled" if succeeded else "failed",
            "actions": actions,
        }

    def _read_state(self) -> dict[str, dict[str, bool]]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _set_enabled(self, gpu_id: int, enabled: bool) -> None:
        state = self._read_state()
        state[str(gpu_id)] = {"enabled": enabled}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            dir=self.state_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary_name, self.state_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

