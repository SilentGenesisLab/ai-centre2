from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.image_upscale_tasks import resolve_model, upscale_images


JOB_ID = "7f1c2a90-2b6e-4d51-9c33-0a5e8b7d4f21"


@contextmanager
def _no_slot(*_args, **_kwargs):
    """替掉并发闸门：本文件测的是任务体本身，闸门由 tests/test_concurrency.py 覆盖。

    不替的话 `.apply()` 会真去连 Redis，单测就变成依赖外部服务了。
    """
    yield {"limit": 1}


def _file(path: Path) -> None:
    """占位文件即可 —— 真读图的是推理包装类，任务体只消费它返回的尺寸。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


SOURCE_SIZE = (64, 48)


class FakeModel:
    """只把「尺寸变了、文件写了」这两件事演出来，任务体关心的就是这两件。"""

    model_name = "seedvr2_ema_3b_fp16.safetensors"

    def __init__(self) -> None:
        self.calls: list[tuple[Path, int]] = []

    def upscale(self, source: Path, target: Path, target_short_side: int):
        self.calls.append((target, target_short_side))
        _file(target)
        return SOURCE_SIZE, (target_short_side, target_short_side)


def _settings(root: Path, max_images: int = 32) -> SimpleNamespace:
    return SimpleNamespace(
        image_upscale_work_dir=root,
        image_upscale_source_dir=root / "src",
        image_upscale_model_dir=root / "weights",
        image_upscale_default_model="3b",
        image_upscale_target_short_side=1080,
        image_upscale_max_images=max_images,
        image_upscale_max_download_bytes=1024 * 1024,
        image_upscale_download_timeout_seconds=30,
    )


class ResolveModelTests(unittest.TestCase):
    def test_auto_uses_configured_default(self) -> None:
        self.assertEqual(resolve_model("auto", "3b"), "3b")
        self.assertEqual(resolve_model("auto", "7b-sharp"), "7b-sharp")

    def test_explicit_name_wins_over_default(self) -> None:
        self.assertEqual(resolve_model("7b-sharp", "3b"), "7b-sharp")

    def test_unknown_name_is_rejected_rather_than_falling_back(self) -> None:
        # 静默退回默认值会让调用方以为在跑 7B、其实一直跑 3B，而且这里也是
        # 「请求方传的名字不许拼成文件系统路径」的那道闸。
        with self.assertRaises(ValueError):
            resolve_model("../../etc/passwd", "3b")
        with self.assertRaises(ValueError):
            resolve_model("seedvr2_ema_7b_sharp_fp8_e4m3fn.safetensors", "3b")


class ImageUpscaleTaskTests(unittest.TestCase):
    def _run(self, settings, request, model=None, download=None, upload=None):
        model = model or FakeModel()
        with (
            patch("control_plane.image_upscale_tasks.get_settings", return_value=settings),
            patch("control_plane.image_upscale_tasks.download_public_media",
                  side_effect=download or self._default_download),
            patch("control_plane.image_upscale_tasks._get_model", return_value=model) as getter,
            patch("control_plane.image_upscale_tasks._upload_file",
                  side_effect=upload or (lambda path, *a, **k: f"https://oss.example/{path.name}")),
            patch("control_plane.image_upscale_tasks.job_slot", _no_slot),
            patch.object(upscale_images, "update_state"),
        ):
            eager = upscale_images.apply(kwargs={"request_data": request}, task_id=JOB_ID, throw=True)
            return eager.get(propagate=True), model, getter

    @staticmethod
    def _default_download(url, directory, stem, *_args, **_kwargs):
        target = directory / f"{stem}.png"
        _file(target)
        return SimpleNamespace(path=target)

    def test_batch_succeeds_and_returns_one_url_per_image(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        request = {"source_uris": ["https://cdn.example/a.png", "https://cdn.example/b.png"],
                   "model": "auto", "target_short_side": 512}
        result, _, _ = self._run(_settings(root), request)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["model"], "3b")
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(result["succeeded_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(len(result["result_urls"]), 2)
        self.assertEqual(result["results"][0]["source_size"], "64x48")
        self.assertEqual(result["results"][0]["output_size"], "512x512")
        self.assertFalse((root / JOB_ID).exists())      # 工作目录必须被清掉
        temporary.cleanup()

    def test_the_model_is_loaded_once_for_the_whole_batch(self) -> None:
        # 这是「只加载一次」的回归闸：一批 N 张只许取一次模型。
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        request = {"source_uris": [f"https://cdn.example/{i}.png" for i in range(5)],
                   "model": "7b-sharp", "target_short_side": 512}
        result, model, getter = self._run(_settings(root), request)

        self.assertEqual(result["succeeded_count"], 5)
        self.assertEqual(getter.call_count, 1)
        self.assertEqual(len(model.calls), 5)
        temporary.cleanup()

    def test_one_bad_download_does_not_fail_the_batch(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        calls = {"n": 0}

        def download(url, directory, stem, *_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("404 not found")
            target = directory / f"{stem}.png"
            _file(target)
            return SimpleNamespace(path=target)

        request = {"source_uris": [f"https://cdn.example/{i}.png" for i in range(3)],
                   "model": "auto", "target_short_side": 512}
        result, model, _ = self._run(_settings(root), request, download=download)

        self.assertEqual(result["status"], "succeeded")     # 整批仍是成功
        self.assertEqual(result["succeeded_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(result["result_urls"]), 2)     # 失败的那张不占位
        self.assertIn("error", result["results"][1])
        self.assertEqual(len(model.calls), 2)               # 坏图不该白跑一次推理
        temporary.cleanup()

    def test_one_bad_inference_does_not_fail_the_batch(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)

        class Flaky(FakeModel):
            def upscale(self, source, target, target_short_side):
                if len(self.calls) == 1:
                    self.calls.append((target, target_short_side))
                    raise RuntimeError("CUDA out of memory")
                return super().upscale(source, target, target_short_side)

        request = {"source_uris": [f"https://cdn.example/{i}.png" for i in range(3)],
                   "model": "auto", "target_short_side": 512}
        result, _, _ = self._run(_settings(root), request, model=Flaky())

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["succeeded_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("CUDA out of memory", result["results"][1]["error"])
        temporary.cleanup()

    def test_too_many_images_is_rejected(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        request = {"source_uris": [f"https://cdn.example/{i}.png" for i in range(3)],
                   "model": "auto", "target_short_side": 512}
        with self.assertRaises(ValueError):
            self._run(_settings(root, max_images=2), request)
        self.assertFalse((root / JOB_ID).exists())
        temporary.cleanup()


class ImageUpscaleApiModelTests(unittest.TestCase):
    def test_defaults(self) -> None:
        from control_plane.api import ImageUpscaleJobRequest

        request = ImageUpscaleJobRequest(source_uris=["https://cdn.example/a.png"])
        self.assertEqual(request.model, "auto")
        self.assertEqual(request.target_short_side, 1080)

    def test_rejects_empty_and_oversized_batches(self) -> None:
        from pydantic import ValidationError

        from control_plane.api import ImageUpscaleJobRequest

        with self.assertRaises(ValidationError):
            ImageUpscaleJobRequest(source_uris=[])
        with self.assertRaises(ValidationError):
            ImageUpscaleJobRequest(source_uris=[f"https://cdn.example/{i}.png" for i in range(65)])

    def test_rejects_unknown_model_and_out_of_range_target(self) -> None:
        from pydantic import ValidationError

        from control_plane.api import ImageUpscaleJobRequest

        for bad in ({"model": "4b"}, {"target_short_side": 100}, {"target_short_side": 5000}):
            with self.assertRaises(ValidationError):
                ImageUpscaleJobRequest(source_uris=["https://cdn.example/a.png"], **bad)


if __name__ == "__main__":
    unittest.main()
