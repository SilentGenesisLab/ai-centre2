from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.api import VideoUpscaleUrlJobRequest, create_video_upscale_job
from control_plane.observability import route_info
from control_plane.video_upscale_tasks import (
    Segment,
    SegmentPlan,
    _process_segment,
    build_payload,
    merge_segments,
    plan_segments,
    provider_order,
    split_video,
)


def fake_segment(index: int = 2, body_frames: int = 334, head_frames: int = 8,
                 tail_real_frames: int = 12) -> Segment:
    plan = SegmentPlan(
        index=index, start_frame=326, body_start=334, body_frames=body_frames,
        head_frames=head_frames, tail_real_frames=tail_real_frames,
        tail_clone_frames=12 - tail_real_frames,
    )
    return Segment(
        plan=plan, upload=Path(f"segment-{index:05d}.mp4"),
        upload_frames=plan.upload_frames, keep_from=head_frames, keep_frames=body_frames,
    )


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

    def test_plan_keeps_bodies_contiguous_and_budgets_the_margins(self) -> None:
        # 正片必须严格首尾相接（不重叠、不留缝），否则拼出来就不是源片。
        plans = plan_segments([300, 300, 50], head_frames=8, tail_frames=12, upload_ceiling_frames=320)
        self.assertEqual([(p.body_start, p.body_frames) for p in plans],
                         [(0, 300), (300, 300), (600, 50)])
        # 首段的段首上下文只能拿自己首帧冻结（start_frame 因此是负的），末段的余量同理。
        self.assertEqual([(p.head_frames, p.tail_real_frames, p.tail_clone_frames) for p in plans],
                         [(8, 12, 0), (8, 12, 0), (8, 0, 12)])
        self.assertEqual([p.start_frame for p in plans], [-8, 292, 592])
        # 上传件 = 上下文 + 正片。
        self.assertEqual([p.upload_frames for p in plans], [320, 320, 70])

    def test_plan_shrinks_the_margin_when_a_cut_overshoots_the_upload_ceiling(self) -> None:
        # 切点跟着关键帧走，实测会有段落多出一帧：354 帧的上传件会变成 355 帧
        # = 11.8333s，超过平台按秒卡的上限。多出来的从段尾余量里扣，正片一帧不动。
        plans = plan_segments([327], head_frames=8, tail_frames=20, upload_ceiling_frames=354)
        self.assertEqual(plans[0].body_frames, 327)
        self.assertEqual(plans[0].tail_real_frames, 0)
        self.assertEqual(plans[0].tail_clone_frames, 19)
        self.assertEqual(plans[0].upload_frames, 354)

    def test_plan_trims_context_when_a_neighbour_is_shorter(self) -> None:
        # 只有相邻段比上下文还短时才借不满：前段短 → 后段头不足；后段短 → 前段尾不足。
        plans = plan_segments([4, 3], head_frames=8, tail_frames=12, upload_ceiling_frames=24)
        self.assertEqual(plans[0].tail_real_frames, 3)
        self.assertEqual(plans[0].tail_clone_frames, 9)
        self.assertEqual(plans[1].head_frames, 4)

    @patch("control_plane.video_upscale_tasks._slice_segment")
    @patch("control_plane.video_upscale_tasks._frame_count")
    @patch("control_plane.video_upscale_tasks._run_ffmpeg")
    def test_splitter_measures_frames_instead_of_seconds(self, run_ffmpeg, frame_count, slice_segment) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            segment_dir = directory / "segments"

            def create_bodies(arguments):
                parts = segment_dir / "parts"
                parts.mkdir(parents=True, exist_ok=True)
                for number in (1, 2):
                    (parts / f"body-{number:05d}.mp4").touch()

            run_ffmpeg.side_effect = create_bodies
            bodies = {segment_dir / "parts" / "body-00001.mp4": 300,
                      segment_dir / "parts" / "body-00002.mp4": 200}
            frame_count.side_effect = lambda path: {
                **bodies,
                segment_dir / "segment-00001.mp4": 320,
                segment_dir / "segment-00002.mp4": 220,
            }[path]
            slice_segment.side_effect = lambda source, filters, frames, target, fps: frames
            segments = split_video(directory / "source.mp4", segment_dir, 11.8, 30.0, 8, 12)

        arguments = run_ffmpeg.call_args_list[0].args[0]
        # 上游的上限算的是上传件时长，所以正片段要先让出上下文的时长：334 帧 = 11.8s。
        self.assertEqual(arguments[arguments.index("-segment_time") + 1], "11.133333")
        self.assertEqual(arguments[arguments.index("-force_key_frames") + 1], "expr:gte(t,n_forced*11.133333)")
        # 首段没有上一段可借，段首上下文是自己的首帧冻结 —— 成片第一帧也就不是冷启动帧。
        # tpad 的 start 只收非负时长，所以按帧率给一个够用的冻结时长（截到 head 帧为止）。
        self.assertEqual(slice_segment.call_args_list[0].args[0].name, "body-00001.mp4")
        self.assertEqual(slice_segment.call_args_list[0].args[1], "tpad=start_mode=clone:start_duration=0.533333")
        # 首段余量借下一段的真实头帧（end_frame=12）。
        self.assertEqual(slice_segment.call_args_list[1].args[0].name, "body-00002.mp4")
        self.assertEqual(slice_segment.call_args_list[1].args[1], "trim=end_frame=12,tpad=stop_mode=clone:stop=-1")
        # 二段头上下文从上一段的尾帧里裁（300-8=292）；末段没有下一段，余量只能拿自己的末帧冻结。
        self.assertEqual(slice_segment.call_args_list[2].args[0].name, "body-00001.mp4")
        self.assertEqual(slice_segment.call_args_list[2].args[1], "trim=start_frame=292")
        self.assertEqual(slice_segment.call_args_list[3].args[1], "trim=start_frame=199,tpad=stop_mode=clone:stop=-1")
        # 每片都要带上输出帧率：只写 passthrough 的话末帧时长会是 0，拼接时接缝挤掉一帧。
        self.assertEqual({call.args[4] for call in slice_segment.call_args_list}, {30.0})
        self.assertEqual(
            [(s.index, s.keep_from, s.keep_frames, s.min_frames, s.upload_frames) for s in segments],
            [(1, 8, 300, 308, 320), (2, 8, 200, 208, 220)],
        )

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
        with patch("control_plane.video_upscale_tasks.imageio_ffmpeg.count_frames_and_secs",
                   return_value=(354, 11.8)):
            result = _process_segment(
                fake_segment(), "https://cdn.example/segment.mp4",
                {"provider": "auto", "max_resolution": 1920},
                "flashvsr_v2,flashvsr,seedvr2", 3, Path("."), 30.0,
            )
        self.assertEqual(result["provider"], "flashvsr")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["index"], 2)
        self.assertEqual(result["shortfall_frames"], 0)

    @patch("control_plane.video_upscale_tasks.download_public_media")
    @patch("control_plane.video_upscale_tasks.run_provider")
    @patch("control_plane.video_upscale_tasks.get_settings")
    def test_segment_retries_when_provider_drops_too_many_frames(self, settings, run_provider, download) -> None:
        # 缺的帧只有上游能补出来，本地补出来的必然是克隆帧 —— 宁可换一家重试。
        settings.return_value = SimpleNamespace(
            video_upscale_max_download_bytes=1024,
            video_upscale_provider_timeout_seconds=60,
        )
        run_provider.side_effect = [("https://cdn.example/short.mp4", "a"),
                                    ("https://cdn.example/ok.mp4", "b")]
        download.return_value = SimpleNamespace(path="result.mp4")
        with patch("control_plane.video_upscale_tasks.imageio_ffmpeg.count_frames_and_secs",
                   side_effect=[(340, 11.33), (354, 11.8)]):
            result = _process_segment(
                fake_segment(), "https://cdn.example/segment.mp4",
                {"provider": "auto", "max_resolution": 1920},
                "flashvsr_v2,flashvsr,seedvr2", 3, Path("."), 30.0,
            )
        self.assertEqual(result["provider"], "flashvsr")
        self.assertEqual(result["provider_frames"], 354)

    @patch("control_plane.video_upscale_tasks.download_public_media")
    @patch("control_plane.video_upscale_tasks.run_provider")
    @patch("control_plane.video_upscale_tasks.get_settings")
    def test_segment_retries_when_provider_fps_is_lower_than_the_source(self, settings, run_provider, download) -> None:
        # 24fps 的结果要凑成 30fps 只能复制帧，正是成片里 20% 重复帧的来源。
        settings.return_value = SimpleNamespace(
            video_upscale_max_download_bytes=1024,
            video_upscale_provider_timeout_seconds=60,
        )
        run_provider.side_effect = [("https://cdn.example/24fps.mp4", "a"),
                                    ("https://cdn.example/30fps.mp4", "b")]
        download.return_value = SimpleNamespace(path="result.mp4")
        with patch("control_plane.video_upscale_tasks.imageio_ffmpeg.count_frames_and_secs",
                   side_effect=[(283, 11.8), (354, 11.8)]):
            result = _process_segment(
                fake_segment(), "https://cdn.example/segment.mp4",
                {"provider": "auto", "max_resolution": 1920},
                "flashvsr_v2,flashvsr,seedvr2", 3, Path("."), 30.0,
            )
        self.assertEqual(result["provider"], "flashvsr")
        self.assertEqual(result["provider_fps"], 30.0)

    @patch("control_plane.video_upscale_tasks._media_metadata")
    def test_merge_refuses_a_provider_result_shorter_than_the_body(self, media_metadata) -> None:
        media_metadata.return_value = {"size": (1920, 1080), "fps": 30.0}
        with TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                merge_segments(
                    [{"provider_frames": 100, "provider_fps": 30.0, "path": "result.mp4"}],
                    [fake_segment()], Path("source.mp4"), Path(temporary), 1920,
                )

    @patch("control_plane.video_upscale_tasks._run_ffmpeg")
    @patch("control_plane.video_upscale_tasks._media_metadata")
    def test_merge_refuses_a_collapsed_timeline(self, media_metadata, run_ffmpeg) -> None:
        # 实测的真事：6 段各带一个零时长末帧时，帧数一帧不少（1801），
        # 时间轴却少 5 帧（59.8667s vs 60.0333s）—— 帧数检查看不出来。
        media_metadata.return_value = {"size": (1920, 1080), "fps": 30.0}
        # 末段只有 131 帧，和实测那次一样：正片合计 5×334 + 131 = 1801 帧。
        segments = [fake_segment(index=index) for index in range(1, 6)]
        segments.append(fake_segment(index=6, body_frames=131, tail_real_frames=0))
        results = [{"provider_frames": 354, "provider_fps": 30.0, "path": "result.mp4"}] * 6
        with TemporaryDirectory() as temporary:
            with patch("control_plane.video_upscale_tasks.imageio_ffmpeg.count_frames_and_secs",
                       return_value=(1801, 59.8667)):
                with self.assertRaises(RuntimeError) as caught:
                    merge_segments(results, segments, Path("source.mp4"), Path(temporary), 1920)
        self.assertIn("spans", str(caught.exception))

    @patch("control_plane.video_upscale_tasks._run_ffmpeg")
    @patch("control_plane.video_upscale_tasks._media_metadata")
    def test_merge_muxes_the_audio_without_truncating_the_video(self, media_metadata, run_ffmpeg) -> None:
        # 源片的音轨比视频轨短一点点是常态；带上 -shortest 就会按音频长度砍掉成片最后一帧
        # （实测 1801 → 1800）。帧数守恒是这一整套改动的底线，收尾这一步也不能破。
        media_metadata.return_value = {"size": (1920, 1080), "fps": 30.0}
        segments = [fake_segment(body_frames=334)]
        results = [{"provider_frames": 354, "provider_fps": 30.0, "path": "result.mp4"}]
        with TemporaryDirectory() as temporary:
            with patch("control_plane.video_upscale_tasks.imageio_ffmpeg.count_frames_and_secs",
                       return_value=(334, 334 / 30)):
                final, frames = merge_segments(results, segments, Path("source.mp4"), Path(temporary), 1920)
        self.assertEqual(frames, 334)
        self.assertEqual(final.name, "upscaled.mp4")
        self.assertNotIn("-shortest", run_ffmpeg.call_args_list[-1].args[0])

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
