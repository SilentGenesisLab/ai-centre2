"""超分的输出尺寸天花板。

实测（2026-10-29，同一段 720x1280 竖屏 VFR 素材）：
  720  ✅ 46s
  1920 ✅ 170s → 1024x1920
  2560 ✅ 200s → 1408x2560
  3840 ❌ 三次尝试全是「工作流运行失败」

所以 RunningHub 那三条按 2560 记天花板；Yotta 的 Starlight 官方支持 4K。
关键是**在提交前就说出来**，而不是让调用方等六七分钟换一句「video upscale failed」。
"""

from __future__ import annotations

import unittest


class CeilingTests(unittest.TestCase):
    def test_runninghub_providers_stop_at_2560(self):
        from control_plane.video_upscale_tasks import _max_output_edge

        for name in ("flashvsr", "flashvsr_v2", "seedvr2"):
            with self.subTest(provider=name):
                self.assertEqual(_max_output_edge(name), 2560)

    def test_yotta_goes_to_4k(self):
        from control_plane.video_upscale_tasks import _max_output_edge

        self.assertEqual(_max_output_edge("starlight26"), 4096)

    def test_large_requests_drop_runninghub_but_keep_yotta(self):
        from control_plane.video_upscale_tasks import providers_for

        # auto 的默认顺序里没有 starlight，所以 4K 一个都不剩
        self.assertEqual(providers_for("auto", "flashvsr_v2,flashvsr,seedvr2", 3840), [])
        # 把 starlight 写进 auto 顺序后，4K 仍然只有它接得住
        self.assertEqual(
            providers_for("auto", "flashvsr_v2,starlight26", 3840), ["starlight26"]
        )
        # 点名 starlight 直接可用
        self.assertEqual(providers_for("starlight26", "flashvsr_v2", 3840), ["starlight26"])

    def test_normal_sizes_keep_the_full_order(self):
        from control_plane.video_upscale_tasks import providers_for

        self.assertEqual(
            providers_for("auto", "flashvsr_v2,flashvsr,seedvr2", 1920),
            ["flashvsr_v2", "flashvsr", "seedvr2"],
        )
        self.assertEqual(
            providers_for("auto", "flashvsr_v2,flashvsr,seedvr2", 2560),
            ["flashvsr_v2", "flashvsr", "seedvr2"],
        )


if __name__ == "__main__":
    unittest.main()
