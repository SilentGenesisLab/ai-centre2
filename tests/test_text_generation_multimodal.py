"""文本通道的多模态输入（Qwen Omni 理解图片/视频/音频）。

背景（2026-11-02）：用户指出「3.8 官方明明可以传视频」。实测确实可以 ——
`video_url` / `input_audio` / `image_url` 三种内容块上游都收。中台原来在三个地方
把素材挡住了：请求模型没有字段（extra=forbid 直接 422）、payload 只拼字符串、
绑定写死 images/videos/audios 全 0。这三条各自有一条测试盯着，避免以后又悄悄退化。
"""

from __future__ import annotations

import unittest

VIDEO = "https://bucket.example.com/a.mp4"
IMAGE = "https://bucket.example.com/a.png"
AUDIO = "https://bucket.example.com/a.mp3"


def _binding(**caps):
    return {"upstream_model": "qwen3.8-omni-flash", "adapter": "dashscope", "capabilities": caps}


def _content(request):
    from control_plane.text_generation_tasks import _payload

    payload = _payload(_binding(), {"max_tokens": 64, **request})
    return payload["messages"][-1]["content"]


class PayloadTests(unittest.TestCase):
    def test_text_only_stays_a_plain_string(self):
        """存量调用方的报文要逐字不变：没有素材就不拼数组。"""
        self.assertEqual(_content({"prompt": "写一句标语"}), "写一句标语")

    def test_video_becomes_a_content_block(self):
        content = _content({"prompt": "描述画面", "reference_video_urls": [VIDEO]})
        self.assertEqual(
            content,
            [{"type": "video_url", "video_url": {"url": VIDEO}},
             {"type": "text", "text": "描述画面"}],
        )

    def test_all_three_modalities_keep_media_before_text(self):
        content = _content({
            "prompt": "问答",
            "reference_image_urls": [IMAGE],
            "reference_video_urls": [VIDEO],
            "reference_audio_urls": [AUDIO],
        })
        self.assertEqual([block["type"] for block in content],
                         ["image_url", "video_url", "input_audio", "text"])
        self.assertEqual(content[1]["video_url"]["url"], VIDEO)
        self.assertEqual(content[-1]["text"], "问答")

    def test_multiple_videos_are_all_sent(self):
        second = "https://bucket.example.com/b.mp4"
        content = _content({"prompt": "比较两段", "reference_video_urls": [VIDEO, second]})
        self.assertEqual([block["video_url"]["url"] for block in content[:2]], [VIDEO, second])


class AudioFormatTests(unittest.TestCase):
    def test_format_follows_the_container_not_the_content_type(self):
        from control_plane.text_generation_tasks import _audio_format

        self.assertEqual(_audio_format("https://x/a.wav"), "wav")
        self.assertEqual(_audio_format("https://x/a.mp3"), "mp3")
        # m4a 是 mp4 容器，百炼认的容器名就是 mp4
        self.assertEqual(_audio_format("https://x/a.m4a"), "mp4")

    def test_query_string_does_not_confuse_the_suffix(self):
        from control_plane.text_generation_tasks import _audio_format

        self.assertEqual(_audio_format("https://x/a.wav?Signature=abc"), "wav")

    def test_unknown_suffix_falls_back_to_mp3(self):
        from control_plane.text_generation_tasks import _audio_format

        self.assertEqual(_audio_format("https://x/speech-3s.wav"), "wav")
        self.assertEqual(_audio_format("https://x/blob"), "mp3")


class RequestModelTests(unittest.TestCase):
    def _model(self):
        from control_plane.api import TextGenerationRequest

        return TextGenerationRequest

    def test_reference_urls_are_trimmed(self):
        request = self._model()(prompt="x", reference_video_urls=[f"  {VIDEO}  ", ""])
        self.assertEqual(request.reference_video_urls, [VIDEO])

    def test_duplicates_after_trimming_are_rejected(self):
        with self.assertRaises(ValueError):
            self._model()(prompt="x", reference_video_urls=[VIDEO, f" {VIDEO} "])

    def test_plain_http_is_rejected_before_the_upstream_ever_sees_it(self):
        """上游是自己去拉 URL，http 与内网地址一律拉不到，报错还很含糊。"""
        with self.assertRaises(ValueError):
            self._model()(prompt="x", reference_video_urls=["http://bucket.example.com/a.mp4"])

    def test_defaults_are_empty_lists(self):
        request = self._model()(prompt="x")
        self.assertEqual(request.reference_image_urls, [])
        self.assertEqual(request.reference_video_urls, [])
        self.assertEqual(request.reference_audio_urls, [])


class CapabilityGateTests(unittest.TestCase):
    """绑定声明了 0 的型号（DeepSeek）带素材必须被判为不兼容，而不是把素材丢掉再照常调用。"""

    def _compatible(self, caps, request):
        from control_plane.generation_tasks import _compatible

        return _compatible({"capabilities": caps, "adapter": "dashscope"}, request)

    def test_text_only_is_compatible_everywhere(self):
        self.assertTrue(self._compatible({"images": 0, "videos": 0, "audios": 0}, {"prompt": "x"}))

    def test_media_needs_a_declaration(self):
        request = {"prompt": "x", "reference_video_urls": [VIDEO]}
        self.assertFalse(self._compatible({"images": 9, "videos": 0, "audios": 3}, request))
        self.assertTrue(self._compatible({"images": 9, "videos": 3, "audios": 3}, request))

    def test_too_many_videos_is_rejected(self):
        request = {"prompt": "x", "reference_video_urls": [VIDEO, VIDEO, VIDEO, VIDEO]}
        self.assertFalse(self._compatible({"images": 9, "videos": 3, "audios": 3}, request))


class SeedTests(unittest.TestCase):
    """老库里两条 Omni 绑定的能力声明是 0/0/0，种子的 INSERT OR IGNORE 不会更新它 ——
    必须有一条幂等 UPDATE 在构造时把老行抬上来。"""

    def test_existing_zero_rows_are_lifted(self):
        import tempfile
        from pathlib import Path

        from control_plane.ai_capabilities import CapabilityStore

        path = Path(tempfile.mkdtemp()) / "capabilities.db"
        store = CapabilityStore(path, "secret")
        # 模拟老库：把两条 Omni 绑定改回 0/0/0，再新建一个 store 触发构造期迁移
        with store._db() as db:
            db.execute(
                "UPDATE model_channels SET capabilities_json=? WHERE model_id IN "
                "(SELECT id FROM models WHERE code IN ('qwen3.5-omni-plus','qwen3.8-omni-flash'))",
                ('{"images":0,"videos":0,"audios":0}',),
            )
        migrated = CapabilityStore(path, "secret")
        declared = {m["code"]: m["input_modalities"] for m in migrated.models()}
        for model in ("qwen3.5-omni-plus", "qwen3.8-omni-flash"):
            caps = migrated.binding(model, "dashscope")["capabilities"]
            self.assertEqual((caps["images"], caps["videos"], caps["audios"]), (9, 3, 3))
            self.assertEqual(declared[model], ["text", "image", "video", "audio"])

    def test_admin_tuned_values_are_not_overwritten(self):
        """管理员手工调过（比如把 videos 调到 1）就别再动它。"""
        import tempfile
        from pathlib import Path

        from control_plane.ai_capabilities import CapabilityStore

        path = Path(tempfile.mkdtemp()) / "capabilities.db"
        store = CapabilityStore(path, "secret")
        with store._db() as db:
            db.execute(
                "UPDATE model_channels SET capabilities_json=? WHERE model_id IN "
                "(SELECT id FROM models WHERE code='qwen3.8-omni-flash')",
                ('{"images":2,"videos":1,"audios":1}',),
            )
        binding = CapabilityStore(path, "secret").binding("qwen3.8-omni-flash", "dashscope")
        self.assertEqual(binding["capabilities"]["videos"], 1)
        self.assertEqual(binding["capabilities"]["images"], 2)


if __name__ == "__main__":
    unittest.main()
