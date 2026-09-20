from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.api import VideoUpscaleUrlJobRequest, create_video_upscale_job
from control_plane.observability import route_info
from control_plane.video_upscale_tasks import _process_segment, build_payload, provider_order, split_video


class VideoUpscaleTests(unittest.TestCase):
    def test_auto_order_and_explicit_provider(self) -> None:
        self.assertEqual(
            provider_order("auto", "flashvsr_v2,flashvsr,seedvr2"),
            ["flashvsr_v2", "flashvsr", "seedvr2"],
        )
        self.assertEqual(provider_order("seedvr2", "flashvsr"), ["seedvr2"])

    def test_payload_uses_provider_specific_nodes(self) -> None:
        flash = build_payload("flashvsr_v2", "https://cdn.example/a.mp4", 1920)
        seed = build_payload("seedvr2", "https://cdn.example/a.mp4", 1088)
        self.assertEqual(flash["nodeInfoList"][0]["nodeId"], "24")
        self.assertEqual(seed["nodeInfoList"][0]["nodeId"], "16")
        self.assertEqual(seed["nodeInfoList"][1]["nodeId"], "71")

    def test_flashvsr_uses_load_video_node_and_file_field(self) -> None:
        payload = build_payload("flashvsr", "https://cdn.example/a.mp4", 1920)
        self.assertEqual(payload["nodeInfoList"][0]["nodeId"], "27")
        self.assertEqual(payload["nodeInfoList"][0]["fieldName"], "file")

    def test_instance_type_follows_provider_load(self) -> None:
        # flashvsr 系列在小卡上会「工作流运行失败」，必须走 plus。
        self.assertEqual(build_payload("flashvsr", "https://cdn.example/a.mp4", 1920)["instanceType"], "plus")
        self.assertEqual(build_payload("flashvsr_v2", "https://cdn.example/a.mp4", 1920)["instanceType"], "plus")
        # seedvr2 负载轻，默认档即可；但超过 1920 仍然升档。
        self.assertEqual(build_payload("seedvr2", "https://cdn.example/a.mp4", 1080)["instanceType"], "default")
        self.assertEqual(build_payload("seedvr2", "https://cdn.example/a.mp4", 2000)["instanceType"], "plus")

    @patch("control_plane.video_upscale_tasks._run_ffmpeg")
    def test_splitter_uses_sub_twelve_second_boundary(self, run_ffmpeg) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            segment_dir = directory / "segments"
            def create_outputs(_arguments):
                segment_dir.mkdir(parents=True, exist_ok=True)
                (segment_dir / "segment-0000.mp4").touch()
                (segment_dir / "segment-0001.mp4").touch()
            run_ffmpeg.side_effect = create_outputs
            result = split_video(directory / "source.mp4", segment_dir, 11.8)
        arguments = run_ffmpeg.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-segment_time") + 1], "11.8")
        self.assertEqual(len(result), 2)

    @patch("control_plane.video_upscale_tasks.download_public_media")
    @patch("control_plane.video_upscale_tasks.run_provider")
    @patch("control_plane.video_upscale_tasks.get_settings")
    def test_segment_falls_back_without_restarting_completed_work(self, settings, run_provider, download) -> None:
        settings.return_value = SimpleNamespace(
            video_upscale_max_download_bytes=1024,
            video_upscale_provider_timeout_seconds=60,
        )
        run_provider.side_effect = [RuntimeError("offline"), ("https://cdn.example/result.mp4", "remote")]
        download.return_value = SimpleNamespace(path="result.mp4")
        result = _process_segment(
            2, "https://cdn.example/segment.mp4",
            {"provider": "auto", "max_resolution": 1920},
            "flashvsr_v2,flashvsr,seedvr2", 3, SimpleNamespace(),
        )
        self.assertEqual(result["provider"], "flashvsr")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["index"], 2)

    def test_route_is_observable(self) -> None:
        info = route_info("POST", "/v1/video-upscale/jobs")
        self.assertEqual(info.service, "upscale")
        self.assertTrue(info.creates_task)


class VideoUpscaleApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_defaults_to_smart_provider(self) -> None:
        job = Mock(id="95d13c67-42c6-4522-bac2-741f4094e58b")
        jobs = Mock()
        jobs.submit.return_value = job
        request = VideoUpscaleUrlJobRequest(source_uri="https://cdn.example/a.mp4")
        with (
            patch("control_plane.api.validate_public_https_url"),
            patch("control_plane.api.get_video_upscale_jobs", return_value=jobs),
        ):
            result = await create_video_upscale_job(request)
        self.assertEqual(result["provider"], "auto")
        self.assertEqual(jobs.submit.call_args.args[0]["max_resolution"], 1920)
        self.assertEqual(jobs.submit.call_args.args[1], 5)


if __name__ == "__main__":
    unittest.main()
