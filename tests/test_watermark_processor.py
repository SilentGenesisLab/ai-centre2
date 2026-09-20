from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from control_plane.watermark_processor import VideoWatermarkProcessor


class FakeWatermarkProcessor(VideoWatermarkProcessor):
    def get_video_duration(self) -> float:
        return 1.0

    def _run_ffmpeg(self, stage: str, arguments: list[str]) -> None:
        target = Path(arguments[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stage.encode())


class FailingWatermarkProcessor(FakeWatermarkProcessor):
    def _run_ffmpeg(self, stage: str, arguments: list[str]) -> None:
        if stage == "color noise":
            raise RuntimeError("failed")
        super()._run_ffmpeg(stage, arguments)


class WatermarkProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"video")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_intensive_processing_keeps_only_final_video(self) -> None:
        output = self.root / "output"
        processor = FakeWatermarkProcessor(self.source, output)

        result = processor.process("intensive")

        self.assertEqual(result.parent, output)
        self.assertRegex(result.name, r"^[0-9a-f]{32}-final_h264_source\.mp4$")
        self.assertTrue(result.is_file())
        self.assertEqual(list((output / "temp").iterdir()), [])

    def test_keep_intermediates_retains_light_pipeline_files(self) -> None:
        output = self.root / "output"
        processor = FakeWatermarkProcessor(
            self.source,
            output,
            keep_intermediates=True,
        )

        result = processor.process("light")

        self.assertEqual(result.parent, output)
        self.assertRegex(result.name, r"^[0-9a-f]{32}-final_light_source\.mp4$")
        intermediates = {path.name for path in (output / "temp").iterdir()}
        self.assertEqual(len(intermediates), 2)
        self.assertTrue(
            any(
                re.fullmatch(
                    r"[0-9a-f]{32}-step1_gaussian_source\.mp4", name
                )
                for name in intermediates
            )
        )
        self.assertTrue(
            any(
                re.fullmatch(
                    r"[0-9a-f]{32}-white_noise_source\.wav", name
                )
                for name in intermediates
            )
        )

    def test_failed_pipeline_still_removes_current_intermediates(self) -> None:
        output = self.root / "output"
        processor = FailingWatermarkProcessor(self.source, output)

        with self.assertRaises(RuntimeError):
            processor.process("intensive")

        self.assertEqual(list((output / "temp").iterdir()), [])

    def test_expired_temp_and_legacy_files_are_removed(self) -> None:
        output = self.root / "output"
        temp_dir = output / "temp"
        temp_dir.mkdir(parents=True)
        expired_temp = temp_dir / "old.mp4"
        expired_legacy = output / "step1_old.mp4"
        fresh = temp_dir / "fresh.mp4"
        for path in (expired_temp, expired_legacy, fresh):
            path.write_bytes(b"video")
        expired_at = time.time() - 90000
        os.utime(expired_temp, (expired_at, expired_at))
        os.utime(expired_legacy, (expired_at, expired_at))

        FakeWatermarkProcessor(self.source, output, retention_seconds=86400)

        self.assertFalse(expired_temp.exists())
        self.assertFalse(expired_legacy.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
