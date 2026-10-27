"""文本生成的两条新链路：百炼 DeepSeek 与 Qwen Omni 的语音输出。

三个判据都来自 2026-10-28 的实测：
* 只有 qwen3.5-omni-plus 能出音频（qwen3.8-omni-flash 带 audio 直接 400）；
* 非流式请求按 audio_tokens 计费但不返回 audio 字段，所以必须流式；
* 上游给的是裸 PCM（int16/单声道/24kHz），没有 RIFF 头，要自己包。
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
import wave
from pathlib import Path


def _sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(event, ensure_ascii=False)}" for event in events] + [
        "data: [DONE]"
    ]


def _delta(**kwargs) -> dict:
    return {"choices": [{"index": 0, "delta": kwargs, "finish_reason": None}]}


class PayloadTests(unittest.TestCase):
    def test_text_only_payload_has_no_streaming_or_modalities(self):
        from control_plane.text_generation_tasks import _payload

        payload = _payload(
            {"upstream_model": "deepseek-v4.1-flash"},
            {"prompt": "写一句标语", "temperature": 0, "max_tokens": 64},
        )
        self.assertNotIn("stream", payload)
        self.assertNotIn("modalities", payload)
        self.assertNotIn("audio", payload)

    def test_audio_payload_forces_streaming(self):
        from control_plane.text_generation_tasks import _payload

        payload = _payload(
            {"upstream_model": "qwen3.5-omni-plus"},
            {"prompt": "打个招呼", "output_audio": True, "voice": "Cherry"},
        )
        self.assertEqual(payload["modalities"], ["text", "audio"])
        self.assertEqual(payload["audio"], {"voice": "Cherry", "format": "wav"})
        # 非流式拿不到音频却照样计费，所以这一个键不能是可选的
        self.assertIs(payload["stream"], True)
        self.assertIs(payload["stream_options"]["include_usage"], True)

    def test_default_voice_is_filled_in(self):
        from control_plane.text_generation_tasks import _payload

        payload = _payload(
            {"upstream_model": "qwen3.5-omni-plus"}, {"prompt": "x", "output_audio": True}
        )
        self.assertEqual(payload["audio"]["voice"], "Tina")


class ConsumeSseTests(unittest.TestCase):
    def test_collects_text_and_audio_in_one_pass(self):
        from control_plane.text_generation_tasks import _consume_sse

        pcm = b"\x00\x01" * 2400  # 0.1s
        # 实测上游是「同一串 base64 的等长切片」，这里照样块切成 4 的倍数
        encoded = base64.b64encode(pcm).decode()
        head, tail = encoded[:2000], encoded[2000:]
        lines = _sse(
            _delta(role="assistant"),
            _delta(content="你好"),
            _delta(audio={"data": head, "id": "a"}),
            _delta(content="，世界"),
            _delta(audio={"data": tail, "id": "a"}),
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 9}},
        )
        body = _consume_sse(lines, "qwen3.5-omni-plus")

        self.assertEqual(body["choices"][0]["message"]["content"], "你好，世界")
        self.assertEqual(body["usage"], {"total_tokens": 9})
        self.assertEqual(body["_audio"]["pcm"], pcm)
        self.assertAlmostEqual(body["_audio"]["duration_seconds"], 0.1, places=2)

    def test_audio_parts_survive_any_chunk_alignment(self):
        """分片边界不该影响还原结果：连续流切片与独立编码分片都要对。"""
        from control_plane.text_generation_tasks import _decode_audio_parts

        pcm = bytes(range(256)) * 30  # 7680 字节，既能整除也能被切歪
        encoded = base64.b64encode(pcm).decode()

        for size in (1, 2, 3, 4, 7, 64, 1000):
            with self.subTest(chunk=size):
                parts = [encoded[i : i + size] for i in range(0, len(encoded), size)]
                self.assertEqual(_decode_audio_parts(parts), pcm)

        # 另一种框法：每块各自独立编码（带 '=' 填充）
        independent = [base64.b64encode(pcm[i : i + 97]).decode() for i in range(0, len(pcm), 97)]
        self.assertEqual(_decode_audio_parts(independent), pcm)

    def test_text_without_audio_is_a_failure(self):
        """拿不到音频必须报错，不能静默返回纯文本 —— 那一单已经按音频计过费。"""
        from control_plane.text_generation_tasks import _consume_sse

        lines = _sse(_delta(content="只有文字"), {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        with self.assertRaises(RuntimeError) as caught:
            _consume_sse(lines, "qwen3.5-omni-plus")
        self.assertIn("no audio", str(caught.exception))

    def test_empty_text_is_a_failure(self):
        from control_plane.text_generation_tasks import _consume_sse

        with self.assertRaises(RuntimeError) as caught:
            _consume_sse(_sse(_delta(content="   ")), "qwen3.5-omni-plus")
        self.assertIn("no text", str(caught.exception))

    def test_upstream_error_event_raises_with_detail(self):
        from control_plane.text_generation_tasks import _consume_sse

        lines = _sse({"error": {"message": "modalities parameter containing audio"}})
        with self.assertRaises(RuntimeError) as caught:
            _consume_sse(lines, "qwen3.8-omni-flash")
        self.assertIn("audio", str(caught.exception))

    def test_broken_lines_are_skipped(self):
        from control_plane.text_generation_tasks import _consume_sse

        pcm = b"\x00\x01" * 10
        lines = [
            "",
            "event: ping",
            "data: {not json",
            *_sse(_delta(content="好"), _delta(audio={"data": base64.b64encode(pcm).decode()})),
        ]
        body = _consume_sse(lines, "m")
        self.assertEqual(body["choices"][0]["message"]["content"], "好")


class WavTests(unittest.TestCase):
    def test_wraps_raw_pcm_with_a_riff_header(self):
        from control_plane.text_generation_tasks import _write_wav

        pcm = b"\x00\x01" * 4800  # 0.2s @24k mono 16bit
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.wav"
            _write_wav(pcm, path)
            self.assertEqual(path.read_bytes()[:4], b"RIFF")
            with wave.open(str(path), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getframerate(), 24000)
                self.assertAlmostEqual(handle.getnframes() / 24000, 0.2, places=3)

    def test_a_real_riff_is_kept_as_is(self):
        from control_plane.text_generation_tasks import _write_wav

        blob = b"RIFF" + b"\x00" * 60
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b.wav"
            _write_wav(blob, path)
            self.assertEqual(path.read_bytes(), blob)


class CompatibilityTests(unittest.TestCase):
    def _binding(self, capabilities: dict) -> dict:
        return {"capabilities": capabilities, "enabled": True, "channel_enabled": True}

    def test_audio_requests_only_reach_bindings_that_declare_it(self):
        from control_plane.generation_tasks import _compatible

        text_only = self._binding({"images": 0, "videos": 0, "audios": 0})
        with_audio = self._binding(
            {"images": 0, "videos": 0, "audios": 0, "audio_output": True}
        )
        request = {"prompt": "x", "output_audio": True}

        self.assertFalse(_compatible(text_only, request))
        self.assertTrue(_compatible(with_audio, request))

    def test_text_only_requests_still_reach_plain_bindings(self):
        from control_plane.generation_tasks import _compatible

        text_only = self._binding({"images": 0, "videos": 0, "audios": 0})
        self.assertTrue(_compatible(text_only, {"prompt": "x", "output_audio": False}))


if __name__ == "__main__":
    unittest.main()
