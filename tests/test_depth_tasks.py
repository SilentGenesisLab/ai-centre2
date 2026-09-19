from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.depth_tasks import infer_video_depth
from control_plane.depth_tasks import effective_input_size


JOB_ID = "4c39cf01-9893-436e-9378-1be045d98f64"


class FakeModel:
    model_name = "Video-Depth-Anything-Small"

    def process(self, _source, target, **kwargs):
        kwargs["progress"]("inferring", 20)
        target.write_bytes(b"depth-video")
        return {
            "frame_count": 24,
            "fps": 24.0,
            "duration_seconds": 1.0,
            "inference_seconds": 0.1,
            "processing_seconds": 0.2,
            "peak_vram_gb": 1.0,
        }


class DepthTaskTests(unittest.TestCase):
    def test_da2_base_input_is_capped_for_production_gpu(self) -> None:
        self.assertEqual(effective_input_size("da2", "base", 518, 392), 392)
        self.assertEqual(effective_input_size("da2", "small", 518, 392), 518)
        self.assertEqual(effective_input_size("da3", "base", 518, 392), 518)

    def test_task_downloads_infers_uploads_and_cleans(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        settings = SimpleNamespace(
            depth_work_dir=root,
            depth_max_download_bytes=1024,
            depth_download_timeout_seconds=30,
            depth_gpu_lock_path=root / "gpu.lock",
            depth_da2_base_max_input_size=392,
        )

        def download(_url, directory, *_args, **_kwargs):
            source = directory / "source.mp4"
            source.write_bytes(b"source")
            return SimpleNamespace(path=source)

        request = {
            "source_uri": "https://cdn.example/source.mp4",
            "version": "da2",
            "model": "small",
            "filename": "depth.mp4",
            "input_size": 518,
            "max_resolution": 960,
            "target_fps": -1,
        }
        with (
            patch("control_plane.depth_tasks.get_settings", return_value=settings),
            patch("control_plane.depth_tasks.download_public_media", side_effect=download),
            patch("control_plane.depth_tasks._get_model", return_value=FakeModel()),
            patch("control_plane.depth_tasks._upload_result", return_value="https://oss.example/depth.mp4"),
            patch.object(infer_video_depth, "update_state"),
        ):
            eager = infer_video_depth.apply(
                kwargs={"request_data": request}, task_id=JOB_ID, throw=True
            )
            result = eager.get(propagate=True)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["version"], "da2")
        self.assertEqual(result["model"], "small")
        self.assertEqual(result["model_name"], "Video-Depth-Anything-Small")
        self.assertEqual(result["effective_input_size"], 518)
        self.assertFalse(result["input_size_capped"])
        self.assertEqual(result["video_url"], "https://oss.example/depth.mp4")
        self.assertFalse((root / JOB_ID).exists())
        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
