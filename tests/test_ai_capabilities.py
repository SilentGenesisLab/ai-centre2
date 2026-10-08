from __future__ import annotations
import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.ai_capabilities import CapabilityStore
import httpx
from fastapi import HTTPException
from control_plane.generation_tasks import (
    _accepted,
    _generate,
    _compatible,
    _error_detail,
    _headers,
    _payload,
    _prepare_video_for_upload,
    _response_body,
    _result,
    _state,
    _sync_image_results,
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
        # runninghub 同样是预设渠道：默认关闭、默认 bearer、**故意不落凭据**
        # （token 走 RUNNINGHUB_API_TOKEN 环境变量，跟超分子系统共用一份）
        self.assertFalse(channels["runninghub"]["enabled"])
        self.assertEqual(channels["runninghub"]["auth_type"],"bearer")
        self.assertFalse(channels["runninghub"]["credential_configured"])
        # teamorouter 也是预设渠道：默认关闭、bearer。它和 runninghub 的差别是**key 可以落库**
        # （控制台 PATCH 进来的是 AESGCM 密文），所以播种期只是把认证方式摆正，密钥留给运维注入。
        self.assertFalse(channels["teamorouter"]["enabled"])
        self.assertEqual(channels["teamorouter"]["auth_type"],"bearer")
        self.assertEqual(channels["teamorouter"]["deployment_type"],"third_party")
        self.assertEqual(channels["teamorouter"]["base_url"],"https://api.teamorouter.com")
        self.assertFalse(channels["teamorouter"]["credential_configured"])
        # 速创（wuyinkeji）也是预设渠道：默认关闭。它与前面几家的差别在**认证形式** ——
        # 上游查询端点只认裸 `Authorization: <key>`，带 Bearer 前缀会回 403，
        # 所以 auth_type 是新增的 authorization 而不是 bearer（2026-10-08 实测）。
        self.assertFalse(channels["wuyinkeji"]["enabled"])
        self.assertEqual(channels["wuyinkeji"]["auth_type"],"authorization")
        self.assertEqual(channels["wuyinkeji"]["deployment_type"],"third_party")
        self.assertEqual(channels["wuyinkeji"]["base_url"],"https://api.wuyinkeji.com")
        self.assertFalse(channels["wuyinkeji"]["credential_configured"])
        self.assertEqual({x["code"] for x in self.store.models()},{"minimax-h3","minimax-h3-rh-enhanced","seedance-2.0","seedance-2.5","gpt-image-2","gpt-image-2.5","gpt-image-2.5-sunburst","gpt-image-2.5-flare","nano-banana-2","nanobanana-2.1","suno-v6","suno-sound","jev"})
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

    def test_seedance_20_480p_is_upgraded_to_720p_on_jmapi(self):
        # 480p 原先被 jmapi 整个排除出候选，后果是这类请求只有 libtv 一家能接；
        # libtv 账号一耗尽就整单失败、连兜底都没有（2026-10-07 实测）。
        # 现在改成把分辨率升到 720p 交给 jmapi：可达性有了，而且是同一个渠道承接，
        # 不会把请求悄悄换到别家。libtv 侧仍原样接 480p，不受影响。
        request = {
            "prompt": "原样提示词",
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "480p",
            "sound": False,
        }
        jm = self.store.binding("seedance-2.0", "jmapi")
        self.assertTrue(_compatible(jm, request))
        self.assertEqual(_payload(jm, request)["video_resolution"], "720p")

        tv = self.store.binding("seedance-2.0", "libtv")
        self.assertTrue(_compatible(tv, request))
        self.assertEqual(_payload(tv, request)["params"]["resolution"], "480p")

    def test_seedance_20_keeps_the_requested_resolution_when_jmapi_can_serve_it(self):
        # 升分辨率的适配只针对 480p —— 720p/1080p 必须原样转发，
        # 别让这段逻辑顺手改掉本来就没问题的请求。
        base = {
            "prompt": "原样提示词",
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "sound": False,
        }
        jm = self.store.binding("seedance-2.0", "jmapi")
        for resolution in ("720p", "1080p"):
            request = dict(base, resolution=resolution)
            self.assertTrue(_compatible(jm, request))
            self.assertEqual(_payload(jm, request)["video_resolution"], resolution)

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
        # 480p 只有 2.5 能做，jmapi 的 2.5 可以。
        # （jmapi 的 **2.0** 现在也能接 480p 请求，但那是靠 _effective_resolution
        #   升到 720p 实现的，不属于本节讨论的 2.5 分辨率切分 ——
        #   见 test_seedance_20_480p_is_upgraded_to_720p_on_jmapi。）
        self.assertTrue(_compatible(jm, resolution_request("480p")))
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

    def test_runninghub_binding_payload_uses_numbered_reference_fields(self):
        """RunningHub 的参考素材是逐张编号的平铺字段，不是数组；上游只收 480p/768p/1080p。"""
        binding = self.store.binding("minimax-h3-rh-enhanced", "runninghub")
        self.assertEqual(binding["upstream_model"], "minimax-h3-rh-enhanced")
        self.assertEqual(binding["channel_code"], "runninghub")
        request = {
            "prompt": "原样提示词",
            "reference_image_urls": ["https://x/a.png", "https://x/b.png"],
            "reference_video_urls": ["https://x/a.mp4"],
            "reference_audio_urls": ["https://x/a.mp3"],
            "duration_seconds": 10,
            "aspect_ratio": "9:16",
            "resolution": "768p",
            "sound": True,
        }
        self.assertTrue(_compatible(binding, request))
        payload = _payload(binding, request)
        self.assertEqual(payload["prompt"], "原样提示词")
        self.assertEqual(payload["refImage1"], "https://x/a.png")
        self.assertEqual(payload["refImage2"], "https://x/b.png")
        self.assertNotIn("refImage3", payload)
        self.assertEqual(payload["refVideo1"], "https://x/a.mp4")
        self.assertEqual(payload["refAudio1"], "https://x/a.mp3")
        self.assertEqual(payload["duration"], 10)
        self.assertEqual(payload["aspectRatio"], "9:16")
        self.assertEqual(payload["resolution"], "768p")
        self.assertEqual(payload["audioMode"], "native")

    def test_runninghub_resolution_and_duration_whitelist(self):
        def request(resolution, duration=10):
            return {
                "reference_image_urls": [],
                "reference_video_urls": [],
                "reference_audio_urls": [],
                "duration_seconds": duration,
                "resolution": resolution,
            }

        binding = self.store.binding("minimax-h3-rh-enhanced", "runninghub")
        for resolution in ("480p", "768p", "1080p"):
            self.assertTrue(_compatible(binding, request(resolution)), resolution)
        # 上游的白名单里没有 720p（实测的原话：allowed values: 480p, 768p, 1080p）
        self.assertFalse(_compatible(binding, request("720p")))
        # 时长是 4～15 的整数
        self.assertFalse(_compatible(binding, request("1080p", duration=3)))
        self.assertTrue(_compatible(binding, request("1080p", duration=4)))
        self.assertTrue(_compatible(binding, request("1080p", duration=15)))
        self.assertFalse(_compatible(binding, request("1080p", duration=16)))

    def test_768p_does_not_leak_to_bindings_without_a_declared_whitelist(self):
        """768p 是本次为新模型新开的取值；没声明白名单的绑定一律不接，
        免得它从新入口漏到 Seedance 上去（那边上游会拒，错误还比我们晚一步）。"""
        request = {
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "duration_seconds": 5,
            "resolution": "768p",
        }
        for channel in ("jmapi", "libtv"):
            for model in ("seedance-2.0", "seedance-2.5"):
                self.assertFalse(
                    _compatible(self.store.binding(model, channel), request),
                    f"{model}:{channel}",
                )

    def test_auto_candidates_come_from_bindings(self):
        """channel="auto" 的候选顺序由绑定表按 priority 给出，不再写死 jmapi/libtv。"""
        self.assertEqual(
            self.store.binding_channels("seedance-2.0"), ["jmapi", "libtv"]
        )
        self.assertEqual(
            self.store.binding_channels("seedance-2.5"), ["jmapi", "libtv"]
        )
        self.assertEqual(
            self.store.binding_channels("minimax-h3-rh-enhanced"), ["runninghub"]
        )

    def test_a_named_channel_is_only_the_head_of_the_candidate_list(self):
        """点名的渠道排头，同一型号的其余渠道按 priority 跟上 —— 报错才有得换。"""
        self.assertEqual(
            self.store.channel_order("gpt-image-2", "teamorouter"), ["teamorouter", "grsai"]
        )
        self.assertEqual(
            self.store.channel_order("gpt-image-2", "grsai"), ["grsai", "teamorouter"]
        )
        self.assertEqual(
            self.store.channel_order("seedance-2.5", "libtv"), ["libtv", "jmapi"]
        )
        # auto 与「没给」都是纯 priority 顺序。
        self.assertEqual(self.store.channel_order("gpt-image-2", "auto"), ["grsai", "teamorouter"])
        self.assertEqual(self.store.channel_order("gpt-image-2", None), ["grsai", "teamorouter"])
        # 只绑了一家的型号没有第二家可换。
        self.assertEqual(self.store.channel_order("nano-banana-2", "grsai"), ["grsai"])
        # 没登记过的名字照样排头：能不能用由 binding() 说了算，这里不替它判断。
        self.assertEqual(
            self.store.channel_order("nano-banana-2", "teamorouter"), ["teamorouter", "grsai"]
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


class TeamORouterTests(unittest.TestCase):
    """TeamORouter：OpenAI 兼容的**同步**生图（三个型号）与 Jev 定型决策。

    这一批绑定的共同点是**没有 task id**：POST 回来就是成品，所以 `query_path` 全是空的，
    走的是 `_generate` 里那条同步分支，而不是「提交 → 轮询」。
    """

    def setUp(self): self.store=CapabilityStore(Path(tempfile.mkdtemp())/"cap.db","unit-secret")

    def test_image_bindings_are_synchronous_and_ranked_after_grsai(self):
        for model in ("gpt-image-2","gpt-image-2.5-sunburst","gpt-image-2.5-flare"):
            binding=self.store.binding(model,"teamorouter")
            self.assertEqual(binding["submit_path"],"/v1/images/generations")
            self.assertEqual(binding["upstream_model"],model)
            # query_path 空 = 永远不会去轮询。同步端点没有 task id，轮询分支对它是死路：
            # `if not tid` 会把它判成「上游没给任务号」而失败，可钱已经花了。
            self.assertEqual(binding["query_path"],"")
            # priority 70 是运维定的「排 grsai(40) 之后当兜底」，所以 auto 的默认选路不变。
            self.assertEqual(binding["priority"],70)
        self.assertEqual(self.store.binding("gpt-image-2","grsai")["priority"],40)

    def test_auto_keeps_grsai_first_and_teamorouter_as_the_fallback(self):
        self.assertEqual(self.store.binding_channels("gpt-image-2"),["grsai","teamorouter"])

    def test_payload_computes_the_size_locally(self):
        binding=self.store.binding("gpt-image-2","teamorouter")
        payload=_payload(binding,{"prompt":"一只在键盘上打字的橘猫","aspect_ratio":"16:9","image_size":"1K","reference_image_urls":[]})
        self.assertEqual(payload,{"model":"gpt-image-2","prompt":"一只在键盘上打字的橘猫","n":1,"size":"1344x768"})
        self.assertEqual(_payload(binding,{"prompt":"x","aspect_ratio":"1:1","image_size":"2K","reference_image_urls":[]})["size"],"2048x2048")

    def test_reference_images_are_not_offered_to_a_text_only_endpoint(self):
        # /v1/images/generations 只吃文本（参考图要走 /v1/images/edits，契约没实测，不猜）。
        request={"prompt":"x","reference_image_urls":["https://x/a.png"]}
        self.assertFalse(_compatible(self.store.binding("gpt-image-2","teamorouter"),request))
        # 同一张单子给 grsai 合法 —— auto 因此会落到 grsai，而不是把参考图悄悄丢掉。
        self.assertTrue(_compatible(self.store.binding("gpt-image-2","grsai"),request))

    def test_jev_binding_uses_the_namespaced_upstream_model(self):
        binding=self.store.binding("jev","teamorouter")
        self.assertEqual(binding["upstream_model"],"typesafe-ai/jev")
        self.assertEqual(binding["submit_path"],"/v1/systemone")
        self.assertEqual(binding["query_path"],"")
        models={m["code"]:m for m in self.store.models()}
        self.assertEqual(models["jev"]["capability_type"],"decision_generation")

    def test_openai_style_error_object_is_drilled_into(self):
        # 两个端点都用 {"error":{"message":...}}：不往下钻的话每条上游报错都只剩 "request rejected"。
        self.assertEqual(_error_detail({"error":{"message":"模型不存在","type":"invalid_request_error"}},"x"),"模型不存在")
        self.assertEqual(_error_detail({"error":{"detail":"bad size"}},"x"),"bad size")


class FakeUpload:
    def __init__(self, uri): self._uri=uri
    def raise_for_status(self): return None
    def json(self): return {"uri":self._uri}


class SyncImageResultTests(unittest.TestCase):
    """内联 b64 → 落盘 → 传内核 → 交付 https 链接。"""

    def setUp(self):
        self.store=CapabilityStore(Path(tempfile.mkdtemp())/"cap.db","unit-secret")
        self.binding=self.store.binding("gpt-image-2","teamorouter")
        self.work=Path(tempfile.mkdtemp());

    def _settings(self):
        settings=Mock()
        settings.video_generation_work_dir=self.work
        settings.kernel_upload_url="https://kernel/upload"
        settings.kernel_api_token="kernel-token"
        settings.video_generation_upload_timeout_seconds=30
        return settings

    @patch("control_plane.generation_tasks.httpx.post")
    @patch("control_plane.generation_tasks.get_settings")
    def test_b64_is_uploaded_as_a_file_then_the_work_dir_is_cleaned(self, get_settings, post):
        seen=[]
        def upload(url, headers=None, data=None, files=None, timeout=None):
            seen.append((files["file"][1].read(), data))
            return FakeUpload("https://oss/result-1.png")
        post.side_effect=upload
        get_settings.return_value=self._settings()

        urls=_sync_image_results(self.binding,{"data":[{"b64_json":base64.b64encode(b"png-bytes").decode()}]},{"external_ref":"ref-1"},"job-1")

        self.assertEqual(urls,["https://oss/result-1.png"])
        self.assertEqual(seen[0][0],b"png-bytes")
        self.assertEqual(seen[0][1]["stage"],"media.image_generation.result_1")
        self.assertEqual(seen[0][1]["external_ref"],"ref-1")
        # 中间文件在 finally 里清掉：一张 1K PNG 是 1.6MB，不留盘。
        self.assertFalse((self.work/"job-1").exists())

    @patch("control_plane.generation_tasks.get_settings")
    def test_url_mode_is_passed_through_without_downloading(self, get_settings):
        urls=_sync_image_results(self.binding,{"data":[{"url":"https://x/a.png"},{"url":"https://x/a.png"}]},{}, "job-1")
        self.assertEqual(urls,["https://x/a.png"])
        # 透传路径完全不该碰设置（更不该去下载转存）。
        get_settings.assert_not_called()

    @patch("control_plane.generation_tasks.get_settings")
    def test_missing_kernel_upload_config_fails_loudly(self, get_settings):
        settings=self._settings(); settings.kernel_upload_url=""
        get_settings.return_value=settings
        with self.assertRaises(RuntimeError): _sync_image_results(self.binding,{"data":[{"b64_json":"eA=="}]},{},"job-1")

    def test_a_response_without_data_is_empty_not_an_exception(self):
        self.assertEqual(_sync_image_results(self.binding,{"error":{"message":"boom"}},{},"job-1"),[])
        self.assertEqual(_sync_image_results(self.binding,{"data":[]},{},"job-1"),[])


class FakeJobStore:
    """`_generate` 需要的那部分能力库：绑定与候选顺序走真库，作业记录放内存。"""

    def __init__(self, capabilities, request):
        self.capabilities, self.request, self.record, self.updates = (
            capabilities, request, {"id": "job-1", "status": "queued"}, [],
        )

    def job(self, job_id, include_request=False):
        out = dict(self.record)
        if include_request:
            out["request"] = self.request
        return out

    def update_job(self, job_id, **values):
        self.updates.append(values)
        self.record.update(values)

    def channel_order(self, model, requested):
        return self.capabilities.channel_order(model, requested)

    def binding(self, model, code):
        return self.capabilities.binding(model, code)


class FakePostClient:
    def __init__(self, responses):
        self.responses, self.urls = responses, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.urls.append(url)
        # 最后一条会一直复用：换渠道时想「第二家一定成功」，只喂最后那一条就行。
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body, self.status_code = body, status_code

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)

    def raise_for_status(self):
        # 轮询分支会调它（生图是同步端点、走不到那里，所以这个假响应原先没有这个方法）。
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class GenerationFallbackTests(unittest.TestCase):
    """点名渠道只是排头：它报错就按 priority 换下一家（同型号的另一家）。"""

    def setUp(self):
        self.capabilities = CapabilityStore(Path(tempfile.mkdtemp()) / "cap.db", "unit-secret")
        for code in ("grsai", "teamorouter"):
            self.capabilities.record_probe(self.capabilities.channel(code)["id"], True)
            self.capabilities.save_channel({"enabled": True}, self.capabilities.channel(code)["id"])

    def _run(self, responses, channel="grsai", sync=None):
        request = {
            "model": "gpt-image-2",
            "channel": channel,
            "prompt": "一只猫",
            "reference_image_urls": [],
            "aspect_ratio": "16:9",
            "image_size": "1K",
        }
        store = FakeJobStore(self.capabilities, request)
        client = FakePostClient(responses)
        settings = Mock(video_generation_blank_image_url="")
        sync_patch = (
            patch("control_plane.generation_tasks._sync_image_results", side_effect=sync)
            if isinstance(sync, Exception)
            else patch("control_plane.generation_tasks._sync_image_results", return_value=sync or ["https://oss/1.png"])
        )
        with (
            patch("control_plane.generation_tasks._store", return_value=store),
            patch("control_plane.generation_tasks.httpx.Client", return_value=client),
            patch("control_plane.generation_tasks.get_settings", return_value=settings),
            sync_patch,
        ):
            _generate(None, "job-1")
        self.store, self.client = store, client
        return store.record

    def test_a_named_channel_falls_back_to_the_other_one_on_error(self):
        # grsai 报 500（点名要的是它），同型号的 teamorouter 还能接 —— 换过去，别整单失败。
        record = self._run([
            FakeResponse({"error": {"message": "upstream is down"}}, status_code=500),
            FakeResponse({"data": [{"b64_json": "eA=="}]}),
        ])

        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["channel_id"], self.capabilities.channel("teamorouter")["id"])
        self.assertEqual(record["fallback_count"], 1)
        self.assertEqual(len(self.client.urls), 2)
        self.assertTrue(self.client.urls[1].endswith("/v1/images/generations"), self.client.urls[1])

    def test_a_model_with_a_single_channel_still_fails_honestly(self):
        # nano-banana-2 只绑了 grsai：没有第二家可换，就如实失败（错误里留着上游那句话）。
        request = {"model": "nano-banana-2", "channel": "grsai", "prompt": "一只猫",
                   "reference_image_urls": [], "aspect_ratio": "1:1", "image_size": "1K"}
        store = FakeJobStore(self.capabilities, request)
        client = FakePostClient([FakeResponse({"error": {"message": "quota exhausted"}}, status_code=429)])
        with (
            patch("control_plane.generation_tasks._store", return_value=store),
            patch("control_plane.generation_tasks.httpx.Client", return_value=client),
            patch("control_plane.generation_tasks.get_settings", return_value=Mock(video_generation_blank_image_url="")),
        ):
            _generate(None, "job-1")

        self.assertEqual(store.record["status"], "failed")
        self.assertIn("quota exhausted", store.record["error"])
        self.assertEqual(len(client.urls), 1)

    def test_a_failure_after_the_image_was_paid_for_does_not_re_buy_it(self):
        # teamorouter 已经出图、转存才炸：换渠道重来等于再买一张，所以只记失败、不再 POST。
        record = self._run([FakeResponse({"data": [{"b64_json": "eA=="}]})],
                           channel="teamorouter", sync=RuntimeError("kernel upload failed"))

        self.assertEqual(record["status"], "failed")
        self.assertIn("kernel upload failed", record["error"])
        self.assertEqual(len(self.client.urls), 1)


class FakePostGetClient(FakePostClient):
    """给基类补一个 `get`。

    libtv 的轮询走的是 GET（jmapi/grsai/runninghub 都走 POST），基类只实现了 post。
    顺带记下每次 POST 的请求体，用来断言「发给 jmapi 的分辨率是升过级的」。
    """

    def __init__(self, posts, gets):
        super().__init__(posts)
        self.gets, self.get_urls, self.bodies = gets, [], []

    def post(self, url, headers=None, json=None):
        self.bodies.append(json)
        return super().post(url, headers=headers, json=json)

    def get(self, url, headers=None):
        self.get_urls.append(url)
        return self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]


class VideoFallsBackToJmapiTests(unittest.TestCase):
    """libtv 不可用时，480p 的 Seedance 2.0 必须能自动切到 jmapi。

    线上真实故障（2026-10-07）：libtv 四个账号余额耗尽，上游回 `算力不足`；而 480p
    当时**只有 libtv 一家能接**（jmapi 被 _compatible 显式排除），于是换渠道逻辑
    无路可走，整单失败。修法是把 480p 升到 720p 交给 jmapi —— 这条测试钉住整个链路：
    提交 → libtv 轮询报失败 → 换 jmapi → 以 720p 提交 → 成功。
    """

    def setUp(self):
        self.capabilities = CapabilityStore(Path(tempfile.mkdtemp()) / "cap.db", "unit-secret")
        for code in ("jmapi", "libtv"):
            self.capabilities.record_probe(self.capabilities.channel(code)["id"], True)
            self.capabilities.save_channel({"enabled": True}, self.capabilities.channel(code)["id"])

    def test_libtv_exhaustion_falls_back_to_jmapi_at_720p(self):
        request = {
            "model": "seedance-2.0", "channel": "libtv", "prompt": "一只猫",
            "reference_image_urls": [], "reference_video_urls": [], "reference_audio_urls": [],
            "duration_seconds": 5, "aspect_ratio": "16:9", "resolution": "480p", "sound": False,
        }
        store = FakeJobStore(self.capabilities, request)
        client = FakePostGetClient(
            posts=[
                FakeResponse({"ok": True, "taskId": "tv-1"}),                                        # libtv 受理
                FakeResponse({"submit_id": "jm-1"}),                                                 # jmapi 受理
                FakeResponse({"status": "succeeded", "result": {"video_url": "https://oss/out.mp4"}}),
            ],
            gets=[FakeResponse({"status": "failed", "msg": "算力不足", "code": 1200000136})],        # libtv 判失败
        )
        with (
            patch("control_plane.generation_tasks._store", return_value=store),
            patch("control_plane.generation_tasks.httpx.Client", return_value=client),
            patch("control_plane.generation_tasks.get_settings", return_value=Mock(video_generation_blank_image_url="")),
            patch("control_plane.generation_tasks._persist_video_results", side_effect=lambda urls, req, jid: urls),
            patch("control_plane.generation_tasks.time.sleep", return_value=None),
        ):
            _generate(None, "job-1")

        self.assertEqual(store.record["status"], "succeeded")
        self.assertEqual(store.record["channel_id"], self.capabilities.channel("jmapi")["id"])
        self.assertEqual(store.record["fallback_count"], 1)

        # 关键断言：真正发给 jmapi 的是 720p。若这里还是 480p，上游会再拒一次。
        jmapi_submit = [
            body for url, body in zip(client.urls, client.bodies)
            if url.endswith("/jmapi/v1/multimodal2video")
        ]
        self.assertEqual(len(jmapi_submit), 1)
        self.assertEqual(jmapi_submit[0]["video_resolution"], "720p")
        self.assertEqual(jmapi_submit[0]["model_version"], "seedance2.0_vip")


class ImageChannelSelectionTests(unittest.TestCase):
    """`channel="auto"` 的候选现在从绑定表读（不再写死 grsai），所以这里钉住选路与拒因。

    单独用真实的能力库（而不是 Mock）来验：绑定表的 priority、capabilities 才是被判的东西。
    """

    def setUp(self):
        self.store=CapabilityStore(Path(tempfile.mkdtemp())/"cap.db","unit-secret")
        self._patches=[
            patch("control_plane.api.get_capability_store",return_value=self.store),
            patch("control_plane.api.validate_public_https_url",return_value=None),
        ]
        for item in self._patches: item.start()

    def tearDown(self):
        for item in self._patches: item.stop()

    def _validate(self, **overrides):
        from control_plane.api import ImageGenerationRequest,_validate_image_generation_request
        request={"model":"gpt-image-2","channel":"teamorouter","prompt":"一只猫","reference_image_urls":[],"aspect_ratio":"1:1","image_size":"1K"}
        request.update(overrides)
        return asyncio.run(_validate_image_generation_request(ImageGenerationRequest(**request)))

    def _reject(self, **overrides):
        with self.assertRaises(HTTPException) as caught: self._validate(**overrides)
        return caught.exception

    def _online(self, code):
        channel=self.store.channel(code)
        self.store.record_probe(channel["id"],True)   # -> health_status = online
        self.store.save_channel({"enabled":True},channel["id"])

    def test_a_text_only_request_is_accepted_but_reference_images_are_not_offered_to_it(self):
        self._online("teamorouter")
        self._validate()  # 纯文本：teamorouter 收
        # /v1/images/generations 不接收参考图（images: 0），所以带图时这个渠道不接这单。
        self.assertEqual(self._reject(reference_image_urls=["https://x/a.png"]).status_code,422)

    def test_auto_moves_a_reference_image_request_to_grsai(self):
        self._online("grsai")
        # teamorouter 根本没启用/在线 —— auto 必须挑到 grsai，而不是把参考图丢掉。
        self._validate(channel="auto",reference_image_urls=["https://x/a.png"])

    def test_auto_uses_teamorouter_as_the_fallback_for_plain_text(self):
        self._online("grsai")
        self._validate(channel="auto")
        # 两组绑定都在线时，auto 的候选顺序仍是 priority 升序：grsai(40) 在前。
        self.assertEqual(self.store.binding_channels("gpt-image-2"),["grsai","teamorouter"])

    def test_a_named_channel_falls_back_when_another_one_serves_the_same_model(self):
        # 点名的 teamorouter 没在线，但同型号的 grsai 在线：这单照样接（worker 先试 teamorouter，
        # 报错/不可用就换 grsai），而不是把调用方挡在门外。
        self._online("grsai")
        self._validate()   # channel="teamorouter"

    def test_the_reject_reason_separates_offline_from_unbound(self):
        # 没探过 = 离线：指定渠道报 503（渠道配了但不可用），比笼统的 422 好排查。
        self.assertEqual(self._reject(channel="teamorouter").status_code,503)
        # auto 没有可用的候选，只能报「没有支持的渠道」。
        self.assertEqual(self._reject(channel="auto").status_code,422)
        # nano-banana-2 没绑 teamorouter：这是「模型没这个渠道」，与掉线是两回事。
        with self.assertRaises(HTTPException) as caught:
            self._validate(model="nano-banana-2",channel="teamorouter")
        self.assertEqual(caught.exception.status_code,404)


class WuyinkejiAdapterTests(unittest.TestCase):
    """速创（wuyinkeji）这一家的协议怪癖，逐条钉住。

    这几条都是 2026-10-08 拿真实请求探出来的，光看代码看不出来：
    参考图必须是逗号串、状态是数字、结果在 data.result 里、认证头不能带前缀。
    """

    def _binding(self, **overrides):
        binding = {"adapter": "wuyinkeji", "upstream_model": "NanoBanana2.1", "credential": "wk-key"}
        binding.update(overrides)
        return binding

    def _request(self, **overrides):
        request = {
            "prompt": "一只猫",
            "reference_image_urls": [],
            "reference_video_urls": [],
            "reference_audio_urls": [],
            "aspect_ratio": "16:9",
            "image_size": "2K",
        }
        request.update(overrides)
        return request

    def test_reference_images_are_joined_into_one_comma_separated_string(self):
        # 传数组上游回 500「转发请求失败」—— 必须是一个逗号串。
        payload = _payload(self._binding(), self._request(
            reference_image_urls=["https://x/a.png", "https://x/b.png"]))

        self.assertEqual(payload["urls"], "https://x/a.png,https://x/b.png")

    def test_no_reference_images_sends_an_empty_string(self):
        # 上游的示例就是 `"urls": ""`，不是 null、不是省略这个键。
        payload = _payload(self._binding(), self._request())

        self.assertEqual(payload["urls"], "")

    def test_the_payload_carries_the_upstream_field_names(self):
        payload = _payload(self._binding(), self._request())

        self.assertEqual(payload["imageSize"], "2K")
        self.assertEqual(payload["aspectRatio"], "16:9")
        self.assertEqual(payload["prompt"], "一只猫")
        self.assertEqual(payload["key"], "wk-key")

    def test_the_raw_authorization_header_is_sent_without_a_bearer_prefix(self):
        headers = _headers({"auth_type": "authorization", "credential": "wk-key"})

        self.assertEqual(headers, {"Authorization": "wk-key"})

    def test_numeric_status_is_translated_for_wuyinkeji(self):
        # 不翻译的话轮询会一直拿到 "2"，既不成功也不失败，白白等到超时。
        self.assertEqual(_state({"data": {"status": 0}}, "wuyinkeji"), "running")
        self.assertEqual(_state({"data": {"status": 2}}, "wuyinkeji"), "succeeded")
        self.assertEqual(_state({"data": {"status": 9}}, "wuyinkeji"), "failed")

    def test_other_adapters_keep_their_english_statuses(self):
        # 翻译只对速创生效，别把别家上游恰好也叫 "2" 的状态带偏。
        self.assertEqual(_state({"data": {"status": "completed"}}), "completed")
        self.assertEqual(_state({"status": "2"}), "2")

    def test_the_result_comes_from_the_nested_data_result(self):
        # 上游把成品放在 `data.result` 里，而 _urls_from_container 原本不认 `result`（单数）。
        urls = _result({"code": 200, "msg": "成功",
                        "data": {"task_id": "image_x", "status": 2,
                                 "result": ["https://o/a.png"]}})

        self.assertEqual(urls, ["https://o/a.png"])

    def test_a_still_pending_task_yields_no_urls(self):
        urls = _result({"code": 200, "data": {"task_id": "image_x", "status": 0, "result": None}})

        self.assertEqual(urls, [])


class WuyinkejiEndToEndTests(unittest.TestCase):
    """速创的完整链路：提交 → 轮询（数字状态）→ 出图。

    单独跑一遍：单测 `_state` 只能证明状态翻译得对，证明不了轮询循环真的用了它 ——
    当初就是这条链路（翻译没接进循环）会让作业一直挂到超时。
    """

    def setUp(self):
        self.capabilities = CapabilityStore(Path(tempfile.mkdtemp()) / "cap.db", "unit-secret")
        channel = self.capabilities.channel("wuyinkeji")["id"]
        self.capabilities.record_probe(channel, True)
        self.capabilities.save_channel({"enabled": True}, channel)
        self.channel_id = channel

    def test_a_wuyinkeji_job_polls_numeric_status_and_succeeds(self):
        request = {
            "model": "nanobanana-2.1", "channel": "wuyinkeji", "prompt": "一只猫",
            "reference_image_urls": [], "aspect_ratio": "16:9", "image_size": "1K",
        }
        store = FakeJobStore(self.capabilities, request)
        client = FakePostGetClient(
            posts=[FakeResponse({"code": 200, "msg": "成功", "data": {"id": "image_x", "count": "1"}})],
            gets=[
                FakeResponse({"code": 200, "data": {"task_id": "image_x", "status": 0, "result": None}}),
                FakeResponse({"code": 200, "data": {"task_id": "image_x", "status": 2,
                                                    "result": ["https://o/a.png"]}}),
            ],
        )
        with (
            patch("control_plane.generation_tasks._store", return_value=store),
            patch("control_plane.generation_tasks.httpx.Client", return_value=client),
            patch("control_plane.generation_tasks.get_settings", return_value=Mock(video_generation_blank_image_url="")),
            patch("control_plane.generation_tasks.time.sleep", return_value=None),
        ):
            _generate(None, "job-1")

        self.assertEqual(store.record["status"], "succeeded")
        self.assertEqual(store.record["channel_id"], self.channel_id)
        self.assertIn("https://o/a.png", store.record["result_urls_json"])
        # 提交用的是 POST，轮询走 GET + `?id=`（速创的查询是 GET）
        self.assertTrue(client.urls[0].endswith("/api/async/NanoBanana2.1"), client.urls[0])
        self.assertIn("/api/async/detail?id=image_x", client.get_urls[0])

