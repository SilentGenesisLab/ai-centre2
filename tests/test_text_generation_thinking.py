"""文本通道的推理开关：为什么默认关闭，以及空正文要说人话。

背景（2026-10-29 排查）：DeepSeek 会把 max_tokens 先花在 reasoning_content 上，
预算烧光时 content 返回空串、finish_reason=length。原来的报错写的是
「响应里没有 choices」，把排查方向带偏了。
"""

from __future__ import annotations

import unittest


def _binding(**caps):
    return {"upstream_model": "deepseek-v4.1-flash", "capabilities": caps}


class ThinkingSwitchTests(unittest.TestCase):
    def test_disabled_by_default_when_the_binding_supports_it(self):
        from control_plane.text_generation_tasks import _payload

        payload = _payload(_binding(thinking_control=True), {"prompt": "写一句标语"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_field_is_omitted_when_the_binding_does_not_declare_support(self):
        """别的型号不认这个字段，发了可能直接 400。"""
        from control_plane.text_generation_tasks import _payload

        payload = _payload(_binding(), {"prompt": "x"})
        self.assertNotIn("thinking", payload)

    def test_caller_can_opt_back_in(self):
        """不发这个字段 == 用上游默认（也就是开着推理）。"""
        from control_plane.text_generation_tasks import _payload

        payload = _payload(_binding(thinking_control=True), {"prompt": "x", "thinking": True})
        self.assertNotIn("thinking", payload)

    def test_audio_and_thinking_coexist(self):
        from control_plane.text_generation_tasks import _payload

        binding = {"upstream_model": "qwen3.5-omni-plus",
                   "capabilities": {"thinking_control": True}}
        payload = _payload(binding, {"prompt": "x", "output_audio": True})
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["thinking"], {"type": "disabled"})


class NoTextReasonTests(unittest.TestCase):
    def _reason(self, body):
        from control_plane.text_generation_tasks import _no_text_reason

        return _no_text_reason(body)

    def test_reasoning_ate_the_budget_names_the_real_cause(self):
        body = {
            "choices": [{"message": {"content": "", "reasoning_content": "在想……"},
                         "finish_reason": "length"}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 676}},
        }
        reason = self._reason(body)
        self.assertIn("推理", reason)
        self.assertIn("676", reason)
        self.assertIn("max_tokens", reason)
        # 不能再出现那句误导人的「没有 choices」
        self.assertNotIn("没有 choices", reason)

    def test_truncated_empty_content(self):
        body = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        self.assertIn("max_tokens", self._reason(body))

    def test_genuinely_missing_choices_still_says_so(self):
        self.assertIn("没有 choices", self._reason({}))

    def test_empty_content_without_reasoning(self):
        body = {"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]}
        self.assertIn("空正文", self._reason(body))


class TempDataRootTests(unittest.TestCase):
    """镜头切分与去水印一提交就 AttributeError 的那个字段。"""

    def test_settings_declares_temp_data_root(self):
        from control_plane.config import Settings

        self.assertIn("temp_data_root", Settings.model_fields)

    def test_temp_media_default_matches(self):
        import temp_media
        from control_plane.config import Settings

        declared = Settings.model_fields["temp_data_root"].default
        self.assertEqual(str(declared), str(temp_media.TEMP_DATA_ROOT))


if __name__ == "__main__":
    unittest.main()
