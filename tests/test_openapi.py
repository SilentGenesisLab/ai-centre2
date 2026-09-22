from __future__ import annotations

import unittest

from control_plane.api import app


class OpenApiTests(unittest.TestCase):
    def test_public_domain_is_the_primary_server(self) -> None:
        schema = app.openapi()
        servers = schema["servers"]

        self.assertEqual(
            servers[0]["url"],
            "https://aicentre2.sligenai.cn:8443",
        )
        self.assertEqual(servers[1]["url"], "http://127.0.0.1:8320")

        paths = schema["paths"]
        lipsync_content = paths["/v1/lipsync/jobs"]["post"]["requestBody"]["content"]
        asr_content = paths["/v1/asr/transcriptions"]["post"]["requestBody"]["content"]
        self.assertEqual(set(lipsync_content), {"application/json"})
        self.assertEqual(set(asr_content), {"application/json"})
        self.assertIn("/v1/ocr/batch", paths)
        self.assertIn("/v1/uploads", paths)
        upload_content = paths["/v1/uploads"]["post"]["requestBody"]["content"]
        self.assertEqual(set(upload_content), {"multipart/form-data"})
        upload_schema = upload_content["multipart/form-data"]["schema"]["$ref"].rsplit("/", 1)[-1]
        self.assertEqual(
            schema["components"]["schemas"][upload_schema]["required"], ["file"]
        )
        self.assertIn("/v1/face-mosaic/jobs", paths)
        self.assertIn("/v1/face-mosaic/jobs/wait", paths)
        self.assertIn("/v1/video-scenes/jobs", paths)
        self.assertIn("/v1/video-scenes/jobs/wait", paths)
        self.assertIn("/v1/watermark-removal/jobs", paths)
        self.assertIn("/v1/watermark-removal/jobs/wait", paths)
        self.assertIn("/v1/video-depth/jobs", paths)
        self.assertIn("/v1/video-depth/jobs/wait", paths)
        self.assertIn("/v1/audio-separation/jobs", paths)
        self.assertIn("/v1/audio-separation/jobs/wait", paths)
        self.assertIn("/v1/video-upscale/jobs", paths)
        self.assertIn("/v1/video-upscale/jobs/wait", paths)
        self.assertIn("/v2/tts/speech/stream", paths)
        self.assertIn("/v2/tts/quality/{request_id}", paths)
        self.assertEqual(
            schema["components"]["schemas"]["TTSJobRequest"]["properties"]["text"]["maxLength"],
            20000,
        )
        self.assertEqual(
            schema["components"]["schemas"]["TTSPublicSpeechRequest"]["properties"]["text"]["maxLength"],
            5000,
        )
        self.assertEqual(
            set(schema["components"]["schemas"]["TTSCloneMode"]["enum"]),
            {"auto", "controllable", "ultimate"},
        )
        self.assertEqual(
            set(schema["components"]["schemas"]["TTSEmotionStrategy"]["enum"]),
            {"auto", "inherit", "force"},
        )

    def test_public_operations_have_chinese_categories_and_names(self) -> None:
        schema = app.openapi()

        self.assertEqual(schema["info"]["title"], "AI Centre 2 企业级 AI 中台接口")
        tag_names = [tag["name"] for tag in schema["tags"]]
        self.assertEqual(
            tag_names,
            [
                "系统状态",
                "文件上传",
                "唇形驱动",
                "语音识别",
                "语音合成",
                "OCR 文字识别",
                "人脸处理",
                "视频切片",
                "AI 视频拉片",
                "水印处理",
                "视频深度推理",
                "通用视频生成",
                "图像生成",
                "音频分离",
                "视频调色",
                "视频超分",
                "MiniMax H3视频生成",
            ],
        )
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                self.assertTrue(operation["tags"])
                self.assertTrue(set(operation["tags"]).issubset(tag_names))
                self.assertRegex(operation["summary"], r"[\u4e00-\u9fff]")

        self.assertEqual(
            schema["paths"]["/v2/tts/speech/stream"]["post"]["summary"],
            "实时流式 VoxCPM2 语音合成",
        )
        self.assertEqual(
            schema["paths"]["/v2/tts/quality/{request_id}"]["get"]["summary"],
            "查询语音合成异步质量审计",
        )

    def test_internal_operations_are_not_in_public_schema(self) -> None:
        paths = app.openapi()["paths"]

        for internal_path in (
            "/v1/asr/transcriptions/upload",
            "/v1/lipsync/jobs/upload",
            "/v1/tts/speech",
            "/v2/tts/speech/upload",
            "/v2/tts/voices/{voice_profile_id}",
            "/v1/admin/gpus",
            "/v1/admin/gpus/{gpu_id}/drain",
            "/v1/admin/gpus/{gpu_id}/disable",
            "/v1/admin/gpus/{gpu_id}/enable",
        ):
            self.assertNotIn(internal_path, paths)

    def test_public_url_requests_have_copyable_examples(self) -> None:
        schema = app.openapi()
        paths = schema["paths"]

        for path in (
            "/v1/asr/transcriptions",
            "/v1/lipsync/jobs",
            "/v2/tts/speech",
            "/v2/tts/speech/stream",
            "/v1/ocr/batch",
            "/v1/face-mosaic/jobs",
            "/v1/face-mosaic/jobs/wait",
            "/v1/video-scenes/jobs",
            "/v1/video-scenes/jobs/wait",
            "/v1/watermark-removal/jobs",
            "/v1/watermark-removal/jobs/wait",
            "/v1/video-depth/jobs",
            "/v1/video-depth/jobs/wait",
            "/v1/audio-separation/jobs",
            "/v1/audio-separation/jobs/wait",
            "/v1/video-generations/minimax-h3/jobs",
        ):
            request_schema = paths[path]["post"]["requestBody"]["content"][
                "application/json"
            ]["schema"]
            reference = request_schema["$ref"].rsplit("/", 1)[-1]
            self.assertTrue(schema["components"]["schemas"][reference]["examples"])


if __name__ == "__main__":
    unittest.main()
