from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.watermark_tasks import remove_video_watermark


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


@contextmanager
def _no_slot(*_args, **_kwargs):
    """替掉并发闸门：本文件测的是任务体，闸门自己由 tests/test_concurrency.py 覆盖。

    不替的话 `.apply()` 会真的去连 Redis，单测就变成依赖外部服务了。
    """
    yield {"limit": 1}


class FakeProcessor:
    def __init__(self, _source, output_dir, **kwargs) -> None:
        self.output_dir = Path(output_dir)
        self.keep_intermediates = kwargs["keep_intermediates"]

    def process(self, mode, progress):
        progress("processing", 50)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.keep_intermediates:
            temp_dir = self.output_dir / "temp"
            temp_dir.mkdir()
            (temp_dir / "step1.mp4").write_bytes(b"intermediate")
        result = self.output_dir / f"{'a' * 32}-final_{mode}.mp4"
        result.write_bytes(b"result")
        return result


class WatermarkTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SimpleNamespace(
            watermark_work_dir=self.root,
            temp_data_root=self.root,
            watermark_intermediate_retention_seconds=86400,
            watermark_max_download_bytes=1024,
            watermark_download_timeout_seconds=30,
            watermark_ffmpeg_bin="ffmpeg",
            watermark_ffmpeg_timeout_seconds=60,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _download(self, _url, directory, *args, **_kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / f"{args[0]}.mp4"
        source.write_bytes(b"source")
        return SimpleNamespace(path=source)

    def _run(self, keep_intermediates: bool):
        request = {
            "source_uri": "https://cdn.example/source.mp4",
            "filename": "result.mp4",
            "mode": "light",
            "keep_intermediates": keep_intermediates,
        }
        with (
            patch("control_plane.watermark_tasks.get_settings", return_value=self.settings),
            patch(
                "control_plane.watermark_tasks.download_public_media",
                side_effect=self._download,
            ),
            patch("control_plane.watermark_tasks.VideoWatermarkProcessor", FakeProcessor),
            patch(
                "control_plane.watermark_tasks._upload_result",
                return_value="https://oss.example/result.mp4",
            ),
            patch("control_plane.watermark_tasks.job_slot", _no_slot),
            patch.object(remove_video_watermark, "update_state"),
        ):
            eager = remove_video_watermark.apply(
                kwargs={"request_data": request},
                task_id=JOB_ID,
                throw=True,
            )
        return eager.get(propagate=True)

    def test_default_task_removes_entire_work_directory(self) -> None:
        result = self._run(False)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(list(self.root.glob(f"*/*-watermark-{JOB_ID}")), [])

    def test_keep_intermediates_removes_source_and_final_only(self) -> None:
        result = self._run(True)
        job_dirs = list(self.root.glob(f"*/*-watermark-{JOB_ID}"))
        self.assertEqual(len(job_dirs), 1)
        job_dir = job_dirs[0]

        self.assertTrue(result["intermediates_retained"])
        self.assertTrue(job_dir.with_name(f".{job_dir.name}.failed.json").is_file())
        self.assertTrue((job_dir / "output" / "temp" / "step1.mp4").is_file())
        self.assertEqual(list(job_dir.glob("*-source.*")), [])
        self.assertEqual(list((job_dir / "output").glob("*-final_*.mp4")), [])


if __name__ == "__main__":
    unittest.main()
