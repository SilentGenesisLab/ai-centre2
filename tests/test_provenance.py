from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.provenance import (
    ID3_DESCRIPTION,
    PNG_KEYWORD,
    apply_provenance,
    build_aigc_payload,
    detect_kind,
    read_jpeg_comment,
    read_mp3_tag,
    read_png_text,
    read_provenance,
    write_jpeg_comment,
    write_mp3_tag,
    write_png_text,
)


def _png_bytes() -> bytes:
    """一张最小的合法 PNG（1x1 RGBA），不依赖 Pillow —— 控制面里也没有它。"""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _png(path: Path) -> None:
    path.write_bytes(_png_bytes())


def _jpeg(path: Path) -> None:
    """最小的 JPEG 外壳：SOI + EOI。

    这里测的是**容器的段结构**，不是解码 —— 我们往 COM 段里写东西、再从 COM 段读回来，
    全程不碰压缩数据，所以不需要一张真图。
    """
    path.write_bytes(b"\xff\xd8\xff\xd9")


def _mp3(path: Path) -> None:
    """最小的 MP3：一个静音帧。读写的其实是它前面的 ID3v2，帧是给探测用的。"""
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)


class ContainerRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_png_round_trip(self) -> None:
        path = self.root / "a.png"
        _png(path)

        write_png_text(path, "hello 中文")
        self.assertEqual(read_png_text(path), "hello 中文")

    def test_png_stays_readable_after_writing(self) -> None:
        # 写进去之后必须还是一张能用的 PNG：IHDR/IDAT/IEND 一个都不能少。
        path = self.root / "a.png"
        _png(path)

        write_png_text(path, "x")

        data = path.read_bytes()
        for kind in (b"IHDR", b"IDAT", b"IEND"):
            self.assertIn(kind, data, f"{kind!r} 丢了")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_jpeg_round_trip(self) -> None:
        path = self.root / "a.jpg"
        _jpeg(path)

        write_jpeg_comment(path, "hello 中文")
        self.assertEqual(read_jpeg_comment(path), "hello 中文")

    def test_mp3_round_trip(self) -> None:
        path = self.root / "a.mp3"
        _mp3(path)

        write_mp3_tag(path, "hello 中文")
        self.assertEqual(read_mp3_tag(path), "hello 中文")

    def test_mp3_keeps_the_audio_frames(self) -> None:
        # 重写 ID3 只该动标签，不该把音频帧吃掉。
        path = self.root / "a.mp3"
        _mp3(path)

        write_mp3_tag(path, "x")

        self.assertTrue(path.read_bytes().endswith(b"\xff\xfb\x90\x00" + b"\x00" * 100))


class IdempotenceTests(unittest.TestCase):
    """反复标注不该越堆越多 —— 读回时拿到哪一条全看运气，那是隐患。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_png_keeps_exactly_one_block(self) -> None:
        path = self.root / "a.png"
        _png(path)

        for index in range(4):
            write_png_text(path, f"v{index}")

        self.assertEqual(path.read_bytes().count(b"iTXt"), 1)
        self.assertEqual(read_png_text(path), "v3")

    def test_jpeg_keeps_exactly_one_comment(self) -> None:
        path = self.root / "a.jpg"
        _jpeg(path)

        for index in range(4):
            write_jpeg_comment(path, f"v{index}")

        self.assertEqual(path.read_bytes().count(b"\xff\xfe"), 1)
        self.assertEqual(read_jpeg_comment(path), "v3")

    def test_mp3_keeps_exactly_one_id3v2_tag(self) -> None:
        path = self.root / "a.mp3"
        _mp3(path)

        for index in range(4):
            write_mp3_tag(path, f"v{index}")

        self.assertEqual(path.read_bytes().count(b"ID3"), 1)
        self.assertEqual(read_mp3_tag(path), "v3")

    def test_png_does_not_grow_without_bound(self) -> None:
        path = self.root / "a.png"
        _png(path)
        write_png_text(path, "x")
        first = path.stat().st_size

        for _ in range(5):
            write_png_text(path, "x")

        self.assertEqual(path.stat().st_size, first)


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_detects_by_content_not_extension(self) -> None:
        # 上游给的文件名经常对不上内容，所以按文件头判。
        mislabeled = self.root / "actually_a_png.mp3"
        _png(mislabeled)

        self.assertEqual(detect_kind(mislabeled), "png")

    def test_unknown_file_reports_nothing_instead_of_raising(self) -> None:
        # 上传接口要能对着任意文件回答「有没有标识」，所以「没有」是正常结果。
        path = self.root / "notes.txt"
        path.write_text("just text", encoding="utf-8")

        self.assertIsNone(detect_kind(path))
        self.assertIsNone(read_provenance(path))

    def test_a_clean_png_reports_nothing(self) -> None:
        path = self.root / "clean.png"
        _png(path)

        self.assertIsNone(read_provenance(path))

    def test_a_foreign_comment_is_reported_but_flagged_unparseable(self) -> None:
        # 别人写的 COM 段不是我们的标识，但也不该假装「没有」——如实报告、标成解不出。
        path = self.root / "foreign.jpg"
        _jpeg(path)
        write_jpeg_comment(path, "written by some other tool")

        found = read_provenance(path)

        self.assertIsNotNone(found)
        self.assertFalse(found["parseable"])
        self.assertEqual(found["raw"], "written by some other tool")


class PayloadTests(unittest.TestCase):
    def _settings(self, **overrides):
        base = {"aigc_content_producer": "思临智创", "aigc_content_propagator": "91310000MA1FL2XXXX",
                "aigc_algorithm_version": "v1.0"}
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_fields_follow_the_spec(self) -> None:
        payload, missing = build_aigc_payload(
            job_id="job-1", external_ref="ref-1", filename="a.png", settings=self._settings())

        aigc = payload["AIGC"]
        self.assertEqual(aigc["Label"], "1")
        self.assertEqual(aigc["ContentProducer"], "思临智创")
        self.assertEqual(aigc["ContentPropagator"], "91310000MA1FL2XXXX")
        self.assertTrue(aigc["ReservedCode2"].startswith("hash:"))
        # 三个 ID 都是 md5（32 位十六进制）
        for key in ("ProduceID", "ReservedCode1", "PropagateID"):
            self.assertRegex(aigc[key], r"^[0-9a-f]{32}$")
        self.assertEqual(missing, [])

    def test_ids_differ_between_jobs(self) -> None:
        first, _ = build_aigc_payload(job_id="job-1", external_ref=None, filename="a.png",
                                      settings=self._settings())
        second, _ = build_aigc_payload(job_id="job-2", external_ref=None, filename="a.png",
                                       settings=self._settings())

        self.assertNotEqual(first["AIGC"]["ProduceID"], second["AIGC"]["ProduceID"])

    def test_missing_company_info_is_reported_not_hidden(self) -> None:
        # 不静默写一份半截载荷：缺什么要说出来。
        payload, missing = build_aigc_payload(
            job_id="job-1", external_ref=None, filename="a.png",
            settings=self._settings(aigc_content_producer="", aigc_content_propagator=""))

        self.assertEqual(sorted(missing), ["ContentProducer", "ContentPropagator"])
        self.assertEqual(payload["AIGC"]["ContentProducer"], "")
        # Label 与本地可推导的字段照常写
        self.assertEqual(payload["AIGC"]["Label"], "1")
        self.assertRegex(payload["AIGC"]["ProduceID"], r"^[0-9a-f]{32}$")


class ApplyProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = SimpleNamespace(
            aigc_content_producer="思临智创", aigc_content_propagator="9131", aigc_algorithm_version="v1")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_marks_a_png_in_place(self) -> None:
        path = self.root / "a.png"
        _png(path)

        report = apply_provenance(path, job_id="job-1", external_ref="r", explicit_label=False,
                                  settings=self.settings, work_dir=self.root)

        self.assertTrue(report["implicit"])
        self.assertFalse(report["explicit"])
        self.assertEqual(report["container"], "png")
        found = read_provenance(path)
        self.assertTrue(found["parseable"])
        self.assertEqual(found["payload"]["AIGC"]["ContentProducer"], "思临智创")

    def test_audio_never_gets_an_explicit_mark(self) -> None:
        # 音频没有公认的可见标识做法：就算请求方传了 explicit_label，也必须不做。
        path = self.root / "a.mp3"
        _mp3(path)

        report = apply_provenance(path, job_id="job-1", external_ref="r", explicit_label=True,
                                  settings=self.settings, work_dir=self.root)

        self.assertFalse(report["explicit"])
        self.assertTrue(report["implicit"])

    def test_unknown_container_is_skipped_with_a_reason(self) -> None:
        path = self.root / "a.txt"
        path.write_text("x", encoding="utf-8")

        report = apply_provenance(path, job_id="job-1", external_ref=None, explicit_label=True,
                                  settings=self.settings, work_dir=self.root)

        self.assertFalse(report["implicit"])
        self.assertFalse(report["explicit"])
        self.assertIsNone(report["container"])
        self.assertIn("unknown container", report["note"])

    def test_unconfigured_company_info_is_noted_in_the_report(self) -> None:
        path = self.root / "a.png"
        _png(path)
        settings = SimpleNamespace(aigc_content_producer="", aigc_content_propagator="",
                                   aigc_algorithm_version="")

        report = apply_provenance(path, job_id="j", external_ref=None, explicit_label=False,
                                  settings=settings, work_dir=self.root)

        self.assertEqual(sorted(report["missing_fields"]), ["ContentProducer", "ContentPropagator"])
        self.assertIn("企业信息未配置", report["note"])

    def test_the_written_payload_is_readable_as_json(self) -> None:
        path = self.root / "a.png"
        _png(path)

        apply_provenance(path, job_id="job-1", external_ref=None, explicit_label=False,
                         settings=self.settings, work_dir=self.root)

        raw = read_png_text(path)
        parsed = json.loads(raw)
        self.assertIn("AIGC", parsed)


if __name__ == "__main__":
    unittest.main()


class PersistHookTests(unittest.TestCase):
    """三个落盘点**确实调了**标识 —— 这条钉子防的是「模块写好了但没接上」。

    接线是最容易静默失效的东西：模块自己的单测全绿，产物却一个标识都没带，
    而且没有任何报错。所以这里直接断言调用发生、并且参数传对了。
    """

    def _kwargs(self):
        return dict(job_id="job-1", external_ref="r", explicit_label=True,
                    settings=SimpleNamespace(), work_dir=Path("."))

    def test_image_results_are_marked_before_upload(self) -> None:
        import base64
        from unittest.mock import Mock, patch
        from control_plane import generation_tasks

        png_b64 = base64.b64encode(_png_bytes()).decode()
        body = {"data": [{"b64_json": png_b64}]}
        response = Mock()
        response.json.return_value = {"uri": "https://oss.example/x.png"}
        response.raise_for_status = Mock()

        with (
            patch("control_plane.generation_tasks.get_settings", return_value=SimpleNamespace(
                kernel_upload_url="https://kernel/upload", kernel_api_token="t",
                video_generation_work_dir=Path(tempfile.mkdtemp()),
                video_generation_upload_timeout_seconds=60)),
            patch("control_plane.generation_tasks.httpx.post", return_value=response),
            patch("control_plane.generation_tasks.apply_provenance") as marker,
        ):
            generation_tasks._sync_image_results(
                {"adapter": "grsai"}, body, {"external_ref": "r", "explicit_label": True}, "job-1")

        self.assertEqual(marker.call_count, 1, "图片产物没有被打标识")
        self.assertEqual(marker.call_args.kwargs["job_id"], "job-1")

    def test_video_results_are_marked_before_upload(self) -> None:
        from unittest.mock import Mock, patch
        from control_plane import generation_tasks

        response = Mock()
        response.json.return_value = {"uri": "https://oss.example/x.mp4"}
        response.raise_for_status = Mock()
        directory = Path(tempfile.mkdtemp())
        local = directory / "result-1.mp4"
        local.write_bytes(b"fake")

        with (
            patch("control_plane.generation_tasks.get_settings", return_value=SimpleNamespace(
                kernel_upload_url="https://kernel/upload", kernel_api_token="t",
                video_generation_work_dir=directory,
                video_generation_result_max_bytes=10**9,
                video_generation_download_timeout_seconds=60,
                video_generation_upload_timeout_seconds=60)),
            patch("control_plane.generation_tasks.download_public_media",
                  return_value=SimpleNamespace(path=local, content_type="video/mp4")),
            patch("control_plane.generation_tasks._prepare_video_for_upload", return_value=local),
            patch("control_plane.generation_tasks.httpx.post", return_value=response),
            patch("control_plane.generation_tasks.apply_provenance") as marker,
        ):
            generation_tasks._persist_video_results(
                ["https://up.example/x.mp4"], {"external_ref": "r", "explicit_label": True}, "job-1")

        self.assertEqual(marker.call_count, 1, "视频产物没有被打标识")

    def test_audio_results_are_marked_and_never_explicit(self) -> None:
        from unittest.mock import Mock, patch
        from control_plane import audio_generation_tasks

        response = Mock()
        response.json.return_value = {"uri": "https://oss.example/x.mp3"}
        response.raise_for_status = Mock()
        directory = Path(tempfile.mkdtemp())
        local = directory / "result-1.mp3"
        local.write_bytes(b"fake")

        with (
            patch("control_plane.audio_generation_tasks.get_settings", return_value=SimpleNamespace(
                kernel_upload_url="https://kernel/upload", kernel_api_token="t",
                audio_generation_work_dir=directory,
                audio_generation_result_max_bytes=10**9,
                audio_generation_download_timeout_seconds=60,
                audio_generation_upload_timeout_seconds=60)),
            patch("control_plane.audio_generation_tasks.download_public_media",
                  return_value=SimpleNamespace(path=local, content_type="audio/mpeg")),
            patch("control_plane.audio_generation_tasks._to_mp3", return_value=local),
            patch("control_plane.audio_generation_tasks.httpx.post", return_value=response),
            patch("control_plane.audio_generation_tasks.apply_provenance") as marker,
        ):
            audio_generation_tasks._persist_audio_results(
                [{"url": "https://up.example/x.mp3"}],
                {"external_ref": "r", "explicit_label": True}, "job-1")

        self.assertEqual(marker.call_count, 1, "音频产物没有被打标识")
        # 音频永远不做显式标识，哪怕请求方传了 true
        self.assertFalse(marker.call_args.kwargs["explicit_label"])


class InspectEndpointTests(unittest.TestCase):
    """核验接口要同时收本地上传与公网 URL，且**二选一**。

    它是页面「内容标识核验」的后端：既要从本地拖一个文件进来，
    也要能贴一条上游产物的直链（很多时候文件就在 OSS 上，人不一定下载得下来）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from control_plane.api import app
        return TestClient(app)

    def test_neither_file_nor_url_is_rejected(self):
        response = self._client().post(
            "/v1/provenance/inspect", headers={"Authorization": "Bearer test"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("必须且只能给一个", response.json()["detail"])

    def test_both_file_and_url_are_rejected(self):
        path = self.root / "a.png"
        _png(path)
        with path.open("rb") as stream:
            response = self._client().post(
                "/v1/provenance/inspect",
                headers={"Authorization": "Bearer test"},
                data={"url": "https://cdn.example/a.png"},
                files={"file": ("a.png", stream, "image/png")},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("必须且只能给一个", response.json()["detail"])

    def test_upload_path_reports_the_marking(self):
        path = self.root / "marked.png"
        _png(path)
        write_png_text(path, '{"AIGC":{"Label":"1"}}')
        with path.open("rb") as stream:
            response = self._client().post(
                "/v1/provenance/inspect",
                headers={"Authorization": "Bearer test"},
                files={"file": ("marked.png", stream, "image/png")},
            )

        body = response.json()
        self.assertTrue(body["found"])
        self.assertEqual(body["source"], "upload")
        self.assertEqual(body["payload"]["AIGC"]["Label"], "1")
        self.assertTrue(body["sha256"])

    def test_url_path_downloads_then_reads(self):
        """URL 分支：服务端去取，再用同一套解析读标识。"""
        from types import SimpleNamespace
        from unittest.mock import patch

        path = self.root / "fetched.png"
        _png(path)
        write_png_text(path, '{"AIGC":{"Label":"1","ContentProducer":"T"}}')

        async def fake_download(*_args, **_kwargs):
            return SimpleNamespace(path=path, size=path.stat().st_size, content_type="image/png")

        with (
            patch("control_plane.api.validate_public_https_url", return_value=None),
            patch("control_plane.api.download_public_media_async", side_effect=fake_download),
        ):
            response = self._client().post(
                "/v1/provenance/inspect",
                headers={"Authorization": "Bearer test"},
                data={"url": "https://cdn.example/fetched.png"},
            )

        body = response.json()
        self.assertTrue(body["found"], body)
        self.assertEqual(body["source"], "url")
        self.assertEqual(body["payload"]["AIGC"]["ContentProducer"], "T")

    def test_a_private_url_is_refused_before_downloading(self):
        # 防 SSRF：公网校验在下载之前，内网地址根本不会去取。
        from control_plane.media_fetch import MediaFetchError
        from unittest.mock import patch

        def refuse(_url):
            raise MediaFetchError(400, "url must be public")

        with patch("control_plane.api.validate_public_https_url", side_effect=refuse):
            response = self._client().post(
                "/v1/provenance/inspect",
                headers={"Authorization": "Bearer test"},
                data={"url": "https://127.0.0.1/a.png"},
            )

        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
