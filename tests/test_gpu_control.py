from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_plane.gpu_control import CommandResult, GpuController, MANAGED_SERVICES


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> CommandResult:
        self.commands.append(command)
        if "show" in command:
            return CommandResult(
                0,
                "ActiveState=active\nSubState=running\nUnitFileState=enabled",
                "",
            )
        return CommandResult(0, "", "")


class GpuControllerTests(unittest.TestCase):
    def test_disable_only_touches_allowlisted_gpu_services(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary_dir:
            controller = GpuController(Path(temporary_dir) / "state.json", runner)
            result = controller.drain(0, disable=True)

        self.assertEqual(result["status"], "disabled")
        touched = [command[-1] for command in runner.commands]
        self.assertEqual(touched, list(MANAGED_SERVICES[0]))
        self.assertNotIn("ai-centre-face-worker-gpu1.service", touched)

    def test_enable_starts_only_requested_gpu(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary_dir:
            controller = GpuController(Path(temporary_dir) / "state.json", runner)
            result = controller.enable(1)

        self.assertEqual(result["status"], "enabled")
        self.assertTrue(all(command[:4] == ["systemctl", "--user", "enable", "--now"] for command in runner.commands))
        self.assertEqual(
            [command[-1] for command in runner.commands],
            list(MANAGED_SERVICES[1]),
        )

    def test_unknown_gpu_is_rejected_without_running_commands(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary_dir:
            controller = GpuController(Path(temporary_dir) / "state.json", runner)
            with self.assertRaises(ValueError):
                controller.drain(3)

        self.assertEqual(runner.commands, [])


if __name__ == "__main__":
    unittest.main()

