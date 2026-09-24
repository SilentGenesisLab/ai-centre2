from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.ai_capabilities import CapabilityStore
import httpx
from control_plane.generation_tasks import (
    _accepted,
    _compatible,
    _error_detail,
    _payload,
    _prepare_video_for_upload,
    _response_body,
    _result,
    _state,
    _task_id,
)

class CapabilityTests(unittest.TestCase):
    def setUp(self): self.store=CapabilityStore(Path(tempfile.mkdtemp())/"cap.db","unit-secret")
    def test_missing_upstream_task_id_does_not_recurse(self):
        self.assertIsNone(_task_id({"status":"failed","message":"unknown model"}))
        self.assertIsNone(_task_id({"data":{}}))
    def test_presets_and_secret_encryption(self):
        channels={x["code"]:x for x in self.store.channels()}
        self.assertFalse(channels["jmapi"]["enabled"]); self.assertFalse(channels["libtv"]["enabled"])
        self.assertEqual(channels["libtv"]["auth_type"],"none")
        # mxapi 是预设渠道：默认关闭、默认 bearer（token 只能从管理端塞，不进仓库）
        self.assertFalse(channels["mxapi"]["enabled"])
        self.assertEqual(channels["mxapi"]["auth_type"],"bearer")
        self.assertFalse(channels["mxapi"]["credential_configured"])
        self.assertEqual({x["code"] for x in self.store.models()},{"minimax-h3","seedance-2.0","seedance-2.5","gpt-image-2","gpt-image-2.5","gpt-image-2.5-sunburst","gpt-image-2.5-flare","nano-banana-2","suno-v6","suno-sound"})
        self.assertFalse(channels["grsai"]["enabled"])
        updated=self.store.save_channel({"credential":"private-value","base_url":"https://example.com"},channels["jmapi"]["id"])
        self.assertEqual(updated["credential_tail"],"alue"); self.assertNotIn("credential",updated)
        self.assertEqual(self.store.channel("jmapi",private=True)["credential"],"private-value")
        self.assertNotIn(b"private-value",self.store.path.read_bytes())
    def test_cannot_enable_before_probe(self):
        channel=self.store.channel("jmapi")
        with self.assertRaises(ValueError): self.store.save_channel({"enabled":True},channel["id"])
        self.store.record_probe(channel["id"],True)
        self.assertTrue(self.store.save_channel({"enabled":True},channel["id"])["enabled"])
    def test_seedance_bindings_and_payloads(self):
        request={"prompt":"原样提示词","reference_image_urls":["https://x/a.png"],"reference_video_urls":[],"reference_audio_urls":[],"duration_seconds":5,"aspect_ratio":"9:16","resolution":"720p","sound":False}
        jm=self.store.binding("seedance-2.0","jmapi"); self.assertTrue(_compatible(jm,request)); self.assertEqual(jm["upstream_model"],"seedance2.0_vip"); self.assertEqual(_payload(jm,request)["prompt"],"原样提示词")
        request["reference_image_urls"]=[];request["reference_video_urls"]=["https://x/a.mp4"]
        tv=self.store.binding("seedance-2.0","libtv"); self.assertTrue(_compatible(tv,request)); self.assertEqual(_payload(tv,request)["videoUrls"],request["reference_video_urls"])

    def test_seedance_20_480p_routes_away_from_jmapi(self):
        request = {
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "resolution": "480p",
        }
        self.assertFalse(
            _compatible(self.store.binding("seedance-2.0", "jmapi"), request)
        )
        self.assertTrue(
            _compatible(self.store.binding("seedance-2.0", "libtv"), request)
        )

    def test_seedance_25_bindings_and_payloads(self):
        request = {
            "prompt": "原样提示词",
            "reference_image_urls": ["https://x/a.png"],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "duration_seconds": 20,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "sound": False,
        }
        jm = self.store.binding("seedance-2.5", "jmapi")
        self.assertEqual(jm["upstream_model"], "seedance2.5")
        self.assertEqual(
            jm["submit_path"], "/jmapi/v1/multimodal2video"
        )
        self.assertTrue(_compatible(jm, request))
        self.assertEqual(_payload(jm, request)["model_version"], "seedance2.5")
        tv = self.store.binding("seedance-2.5", "libtv")
        self.assertEqual(tv["upstream_model"], "star-video2.5")
        self.assertEqual(
            tv["query_path"], "/libtv/api/v1/video/query/{task_id}"
        )
        self.assertTrue(_compatible(tv, request))
        self.assertEqual(_payload(tv, request)["model"], "star-video2.5")

    def test_seedance_25_resolution_split_between_channels(self):
        def resolution_request(resolution, duration=5):
            return {
                "reference_image_urls": [],
                "reference_video_urls": [],
                "reference_audio_urls": [],
                "duration_seconds": duration,
                "resolution": resolution,
            }

        jm = self.store.binding("seedance-2.5", "jmapi")
        tv = self.store.binding("seedance-2.5", "libtv")
        # 480p 只有 2.5 能做，jmapi 的 2.5 可以，jmapi 的 2.0 不行。
        self.assertTrue(_compatible(jm, resolution_request("480p")))
        self.assertFalse(
            _compatible(
                self.store.binding("seedance-2.0", "jmapi"),
                resolution_request("480p"),
            )
        )
        # 1080p 上游只认 libtv。
        self.assertFalse(_compatible(jm, resolution_request("1080p")))
        self.assertTrue(_compatible(tv, resolution_request("1080p")))

    def test_duration_ceiling_is_per_binding(self):
        def duration_request(duration):
            return {
                "reference_image_urls": [],
                "reference_video_urls": [],
                "reference_audio_urls": [],
                "duration_seconds": duration,
                "resolution": "720p",
            }

        for channel in ("jmapi", "libtv"):
            self.assertTrue(
                _compatible(
                    self.store.binding("seedance-2.0", channel),
                    duration_request(15),
                )
            )
            self.assertFalse(
                _compatible(
                    self.store.binding("seedance-2.0", channel),
                    duration_request(16),
                )
            )
            self.assertTrue(
                _compatible(
                    self.store.binding("seedance-2.5", channel),
                    duration_request(30),
                )
            )

    def test_reference_image_cap_is_per_binding(self):
        request = {
            "reference_image_urls": [f"https://x/{i}.png" for i in range(12)],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "duration_seconds": 5,
            "resolution": "720p",
        }
        for channel in ("jmapi", "libtv"):
            self.assertFalse(
                _compatible(self.store.binding("seedance-2.0", channel), request)
            )
            self.assertTrue(
                _compatible(self.store.binding("seedance-2.5", channel), request)
            )

    def test_text_only_seedance_injects_blank_reference_image(self):
        request = {
            "prompt": "A minimal product commercial",
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "sound": False,
        }
        blank = "https://assets.example.com/blank.png"
        for channel in ("jmapi", "libtv"):
            binding = self.store.binding("seedance-2.0", channel)
            payload = _payload(binding, request, blank)
            key = "image_urls" if channel == "jmapi" else "imageUrls"
            self.assertEqual(payload[key], [blank])
        self.assertEqual(request["reference_image_urls"], [])

    def test_explicit_media_does_not_inject_blank_reference_image(self):
        request = {
            "prompt": "test",
            "reference_image_urls": [],
            "reference_video_urls": ["https://assets.example.com/ref.mp4"],
            "reference_audio_urls": [],
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "sound": False,
        }
        for channel in ("jmapi", "libtv"):
            binding = self.store.binding("seedance-2.0", channel)
            payload = _payload(binding, request, "https://assets.example.com/blank.png")
            key = "image_urls" if channel == "jmapi" else "imageUrls"
            self.assertEqual(payload[key], [])

    def test_jmapi_nested_result_is_parsed(self):
        body = {
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "submit_id": "upstream-1",
                    "gen_status": "completed",
                    "result_json": {
                        "videos": [
                            {"video_url": "https://cdn.example.com/result.mp4"}
                        ]
                    },
                }
            ),
        }
        self.assertTrue(_accepted(body))
        self.assertEqual(_task_id(body), "upstream-1")
        self.assertEqual(_state(body), "completed")
        self.assertEqual(
            _result(body), ["https://cdn.example.com/result.mp4"]
        )

    def test_libtv_nested_status_and_result_are_parsed(self):
        body = {
            "ok": True,
            "task": {
                "id": "upstream-2",
                "status": "SUCCESS",
                "result": {
                    "urls": ["https://cdn.example.com/result.mp4"]
                },
            },
        }
        self.assertTrue(_accepted(body))
        self.assertEqual(_task_id(body), "upstream-2")
        self.assertEqual(_state(body), "success")
        self.assertEqual(
            _result(body), ["https://cdn.example.com/result.mp4"]
        )

    def test_upstream_rejection_and_libtv_query_migration(self):
        self.assertFalse(_accepted({"ok": False, "message": "quota exhausted"}))
        self.assertFalse(_accepted({"exit_code": 1, "stderr": "invalid input"}))
        self.assertEqual(
            _error_detail({"ok": False, "message": "quota exhausted"}, "fallback"),
            "quota exhausted",
        )
        self.assertEqual(
            _error_detail(
                {"stdout": json.dumps({"fail_reason": "unsupported resolution"})},
                "fallback",
            ),
            "unsupported resolution",
        )
        binding = self.store.binding("seedance-2.0", "libtv")
        self.assertEqual(
            binding["query_path"],
            "/libtv/api/v1/video/query/{task_id}",
        )
        self.assertEqual(binding["capabilities"]["images"], 9)

    def test_sound_true_keeps_original_result_file(self):
        path = Path(tempfile.mkdtemp()) / "result.mp4"
        self.assertIs(_prepare_video_for_upload(path, {"sound": True}), path)

    @patch("control_plane.generation_tasks.shutil.which", return_value="ffmpeg")
    @patch("control_plane.generation_tasks.subprocess.run")
    def test_sound_false_removes_audio_without_video_reencode(
        self, run, _which
    ):
        path = Path(tempfile.mkdtemp()) / "result.mp4"
        path.write_bytes(b"source")

        def create_output(command, **_kwargs):
            Path(command[-1]).write_bytes(b"silent-video")

        run.side_effect = create_output
        output = _prepare_video_for_upload(path, {"sound": False})
        command = run.call_args.args[0]
        self.assertIn("-an", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(output.read_bytes(), b"silent-video")
    def test_grsai_sse_compatible_json(self):
        response=httpx.Response(200,text='data: {"id":"task-1","status":"succeeded","results":[{"url":"https://x/result.png"}]}')
        body=_response_body(response)
        self.assertEqual(_result(body),["https://x/result.png"])
        binding=self.store.binding("gpt-image-2.5","grsai")
        payload=_payload(binding,{"prompt":"原样","aspect_ratio":"1:1","reference_image_urls":[]})
        self.assertTrue(payload["shutProgress"])
    def test_nano_banana_payload_uses_dedicated_contract(self):
        binding=self.store.binding("nano-banana-2","grsai")
        request={"prompt":"test","aspect_ratio":"16:9","image_size":"1K","reference_image_urls":[]}
        self.assertEqual(binding["submit_path"],"/v1/draw/nano-banana")
        self.assertEqual(_payload(binding,request)["imageSize"],"1K")
    def test_gpt_image_variants_use_pixel_dimensions(self):
        request={"prompt":"test","aspect_ratio":"16:9","image_size":"1K","reference_image_urls":[]}
        for model in ("gpt-image-2.5-sunburst","gpt-image-2.5-flare"):
            self.assertEqual(_payload(self.store.binding(model,"grsai"),request)["aspectRatio"],"1344x768")
