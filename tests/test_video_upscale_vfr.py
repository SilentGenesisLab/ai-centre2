"""切段时必须钉死帧率 —— 否则 VFR 源会被按 tbr 重编码、帧数膨胀。

2026-10-29 实测：720x1280 竖屏、193 帧 / 8.01s、容器声明 `24.07 fps, 60 tbr`。
不钉 `-r` 时 ffmpeg 按 60 重编码，同一段变成 481 帧；下游每一处帧账都跟着错，
最终在合并校验里以「expected 481 / got 154」失败，表现为「视频超分走不通」。
"""

from __future__ import annotations

import unittest
from unittest import mock


class _StopAfterSegment(Exception):
    """切完正片段就把流程掐断，避免测试往下跑拼接步骤。"""


class SplitPinsFrameRateTests(unittest.TestCase):
    def _commands(self):
        """跑 split_video，把交给 ffmpeg 的命令全抓下来。

        只关心**第一条**命令（切正片段的那条）。后面的拼接/切片步骤用桩跳过 ——
        这一版要守的是「切段时钉住帧率」，不是整条流水线。
        """
        import control_plane.video_upscale_tasks as tasks

        captured: list[list[str]] = []

        def fake_run(arguments):
            captured.append(list(arguments))
            if "-f" in arguments and "segment" in arguments:
                # 切完片就让流程停下：后面的上下文拼接不在本测试范围
                raise _StopAfterSegment

        with mock.patch.object(tasks, "_run_ffmpeg", side_effect=fake_run), \
             mock.patch.object(tasks.Path, "mkdir"), \
             mock.patch.object(tasks.Path, "unlink"):
            with self.assertRaises(_StopAfterSegment):
                tasks.split_video(
                    tasks.Path("/tmp/source.mp4"), tasks.Path("/tmp/out"),
                    segment_seconds=11.8, fps=24.07,
                    head_frames=8, tail_frames=20,
                )
        return captured

    def test_segment_command_pins_the_source_frame_rate(self):
        commands = self._commands()
        segment_cmd = next(c for c in commands if "-f" in c and "segment" in c)
        self.assertIn("-r", segment_cmd, "切段命令必须带 -r，否则 VFR 源会按 tbr 膨胀")
        rate = segment_cmd[segment_cmd.index("-r") + 1]
        self.assertEqual(float(rate), 24.07)

    def test_segment_command_forces_cfr(self):
        commands = self._commands()
        segment_cmd = next(c for c in commands if "-f" in c and "segment" in c)
        self.assertIn("-fps_mode", segment_cmd)
        self.assertEqual(segment_cmd[segment_cmd.index("-fps_mode") + 1], "cfr")

    def test_frame_budget_is_computed_from_the_real_fps(self):
        """11.8s 上限 @24.07fps = 284 帧，扣掉首尾上下文 8+20 → 正片 256 帧。"""
        commands = self._commands()
        segment_cmd = next(c for c in commands if "-f" in c and "segment" in c)
        self.assertEqual(segment_cmd[segment_cmd.index("-g") + 1], "256")


if __name__ == "__main__":
    unittest.main()
