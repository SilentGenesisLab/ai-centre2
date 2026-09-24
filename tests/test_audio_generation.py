from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.api import AudioGenerationRequest, _validate_audio_generation_request
from control_plane.audio_generation_jobs import AudioGenerationJobClient
from control_plane.audio_generation_tasks import (
    _accepted,
    _payload,
    _poll_songs,
    _song,
    _task_ids,
    _task_state,
)
from control_plane.observability import route_info
from fastapi import HTTPException

MUSIC_BINDING = {"adapter": "mxapi", "upstream_model": "chirp-hawk"}
SOUND_BINDING = {"adapter": "mxapi", "upstream_model": "chirp-crow"}

# 这两个响应体是 2026-09-23 实测响应里裁出来的（只留解析用得到的字段），关键点是
# **同一个 job 的两条 task 里 `extend` 都是两首**，而自己那一首要靠 custom_id 对。
SIBLING_A = {
    "code": "200",
    "message": "success",
    "data": {
        "task_id": "139830437",
        "status": "completed",
        "points_cost": "18",
        "error": "None",
        "result": {
            "custom_id": "46595f83-e1d3-481c-932c-a7307e9eb1d1",
            "status": "3",
            "fileInfo": {"duration": "262", "mp3Url": "http://cn.m4a.bmnmny.cn/46595f83-e1d3-481c-932c-a7307e9eb1d1.m4a"},
            "proxy_url": "https://open.mxapi.org/api/v2/proxy/resource?url=http%3A%2F%2Fcn.m4a",
            "extend": json.dumps([
                {
                    "id": "46595f83-e1d3-481c-932c-a7307e9eb1d1",
                    "title": "长安谣",
                    "model_name": "chirp-hawk",
                    "image_url": "https://cdn2.suno.ai/image_46595f83.jpeg",
                    "audio_url": "http://cn.m4a.bmnmny.cn/46595f83-e1d3-481c-932c-a7307e9eb1d1.m4a",
                    "media_urls": [{"url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/46595f83.m4a", "content_type": "m4a-opus"}],
                    "metadata": {"prompt": "[Verse]\n长安月", "tags": "cinematic chinese folk"},
                },
                {
                    "id": "9f4c2e50-40ec-4d47-a25d-4b5e8ce63e95",
                    "title": "长安谣",
                    "model_name": "chirp-hawk",
                    "image_url": "https://cdn2.suno.ai/image_9f4c2e50.jpeg",
                    "media_urls": [{"url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/9f4c2e50.m4a", "content_type": "m4a-opus"}],
                    "metadata": {"prompt": "[Verse]\n长安月", "tags": "cinematic chinese folk"},
                },
            ]),
        },
    },
}
# 兄弟 task：**它自己那一首在 extend[1]**，照 extend[0] 取就会把上面那首记成自己的。
SIBLING_B = {
    "code": "200",
    "message": "success",
    "data": {
        "task_id": "139830438",
        "status": "completed",
        "error": "None",
        "result": {
            "custom_id": "9f4c2e50-40ec-4d47-a25d-4b5e8ce63e95",
            "status": "3",
            "fileInfo": {"duration": "256.4", "mp3Url": "http://cn.m4a.bmnmny.cn/9f4c2e50-40ec-4d47-a25d-4b5e8ce63e95.m4a"},
            "extend": SIBLING_A["data"]["result"]["extend"],
        },
    },
}
PROCESSING = {"code": "200", "message": "success", "data": {"task_id": "1", "status": "processing", "error": "None", "result": {"status": "2"}}}
FAILED = {"code": "200", "message": "success", "data": {"task_id": "2", "status": "failed", "error": "None", "result": {"status": "4", "errormsg": "generation failed"}}}


def music_request(**overrides):
    base = {
        "model": "suno-v6",
        "prompt": "",
        "lyrics": "",
        "tags": "",
        "title": "",
        "instrumental": False,
        "vocal_gender": None,
        "style_weight": None,
        "weirdness_constraint": None,
        "music_model": "chirp-hawk",
        "sound_model": "chirp-crow",
        "loop": False,
    }
    base.update(overrides)
    return base


class JobClientTests(unittest.TestCase):
    @patch("control_plane.generation_jobs.GenerationJobClient._app")
    def test_submit_lands_on_the_audio_queue(self, app: Mock) -> None:
        store = Mock()
        store.create_job.return_value = "job-1"
        client = AudioGenerationJobClient(Mock(), store)

        client.submit(music_request(prompt="长安", channel="mxapi"))

        store.create_job.assert_called_once()
        self.assertEqual(store.create_job.call_args.args[0], "suno-v6")
        self.assertEqual(store.create_job.call_args.args[1], "mxapi")
        app.return_value.send_task.assert_called_once_with(
            "control_plane.audio_generation",
            kwargs={"job_id": "job-1"},
            task_id="job-1",
            queue="audio_generation",
        )


class PayloadTests(unittest.TestCase):
    def test_inspiration_mode_sends_a_description_only(self) -> None:
        payload = _payload(MUSIC_BINDING, music_request(prompt="一首关于长安的民谣", title="长安谣"))

        self.assertEqual(payload["gpt_description_prompt"], "一首关于长安的民谣")
        self.assertEqual(payload["mv"], "chirp-hawk")
        self.assertEqual(payload["title"], "长安谣")
        self.assertFalse(payload["make_instrumental"])
        self.assertNotIn("prompt", payload)
        self.assertNotIn("tags", payload)

    def test_custom_mode_sends_lyrics_instead_of_a_description(self) -> None:
        payload = _payload(
            MUSIC_BINDING,
            music_request(lyrics="[Verse]\n长安月", tags="chinese folk", title="长安谣"),
        )

        self.assertEqual(payload["prompt"], "[Verse]\n长安月")
        self.assertEqual(payload["tags"], "chinese folk")
        self.assertNotIn("gpt_description_prompt", payload)

    def test_instrumental_and_sliders(self) -> None:
        payload = _payload(
            MUSIC_BINDING,
            music_request(prompt="古筝独奏", instrumental=True, style_weight=0.8, vocal_gender="f"),
        )

        self.assertTrue(payload["make_instrumental"])
        self.assertEqual(payload["metadata"]["vocal_gender"], "f")
        # 只回传调用方真的给了的滑块，不替上游编默认值。
        self.assertEqual(payload["metadata"]["control_sliders"], {"style_weight": 0.8})

    def test_sound_payload_has_no_music_fields(self) -> None:
        payload = _payload(
            SOUND_BINDING,
            {
                "model": "suno-sound",
                "sound_model": "chirp-fenix",
                "title": "Rain",
                "tags": "steady rain on a wooden roof",
                "loop": True,
            },
        )

        self.assertEqual(
            payload,
            {"mv": "chirp-fenix", "title": "Rain", "tags": "steady rain on a wooden roof", "loop": True},
        )


class ParserTests(unittest.TestCase):
    def test_accepted_reads_the_string_code(self) -> None:
        self.assertTrue(_accepted({"code": "200"}))
        self.assertTrue(_accepted({"code": 200}))
        self.assertFalse(_accepted({"code": "400", "message": "参数验证失败"}))
        self.assertFalse(_accepted({"code": "500"}))
        self.assertFalse(_accepted({"ok": False}))

    def test_task_ids_are_both_tasks(self) -> None:
        self.assertEqual(
            _task_ids({"code": "200", "data": {"task_ids": ["139831293", "139831294"]}}),
            ["139831293", "139831294"],
        )
        self.assertEqual(_task_ids({"data": {"task_id": "123"}}), ["123"])
        self.assertEqual(_task_ids({"data": {}}), [])

    def test_task_state_never_reads_the_none_string_as_an_error(self) -> None:
        # mxapi 把「没有错误」写成字符串 "None"：拿真值判断会把每条正常响应都当成失败。
        self.assertEqual(_task_state(SIBLING_A), "completed")
        self.assertEqual(_task_state(PROCESSING), "processing")
        self.assertEqual(_task_state(FAILED), "failed")
        self.assertEqual(_task_state({"data": {"error": "boom"}}), "failed")

    def test_song_claims_its_own_clip(self) -> None:
        first, second = _song(SIBLING_A), _song(SIBLING_B)

        self.assertEqual(first["clip_id"], "46595f83-e1d3-481c-932c-a7307e9eb1d1")
        self.assertEqual(second["clip_id"], "9f4c2e50-40ec-4d47-a25d-4b5e8ce63e95")
        # 照 extend 的先后顺序取，两条 task 会回报同一首（就是 SIBLING_B 那一支反例）。
        self.assertNotEqual(first["url"], second["url"])
        self.assertEqual(first["url"], "https://d2lwuy8qc234o3.cloudfront.net/1/clip/46595f83.m4a")
        self.assertEqual(first["duration_seconds"], 262.0)
        self.assertEqual(first["title"], "长安谣")
        self.assertEqual(first["cover_url"], "https://cdn2.suno.ai/image_46595f83.jpeg")
        self.assertEqual(first["lyrics"], "[Verse]\n长安月")

    def test_song_falls_back_to_the_mp3_basename(self) -> None:
        body = json.loads(json.dumps(SIBLING_B))
        del body["data"]["result"]["custom_id"]

        self.assertEqual(_song(body)["clip_id"], "9f4c2e50-40ec-4d47-a25d-4b5e8ce63e95")

    def test_song_without_a_matching_entry_is_skipped(self) -> None:
        body = json.loads(json.dumps(SIBLING_A))
        body["data"]["result"]["custom_id"] = "unknown-clip"
        body["data"]["result"]["fileInfo"]["mp3Url"] = "http://x/unknown-clip.m4a"

        self.assertIsNone(_song(body))

    def test_song_falls_back_to_the_https_proxy_when_media_urls_are_missing(self) -> None:
        body = json.loads(json.dumps(SIBLING_A))
        entries = json.loads(body["data"]["result"]["extend"])
        for entry in entries:
            entry.pop("media_urls")
        body["data"]["result"]["extend"] = json.dumps(entries)

        url = _song(body)["url"]
        self.assertTrue(url.startswith("https://open.mxapi.org/api/v2/proxy/resource"), url)


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body, self.status_code = body, status_code

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("unexpected status")


class FakeClient:
    """按 task_id 查表回响应，并记下请求过的 URL（用来验证 ?id={task_id} 拼对了）。"""

    def __init__(self, by_task: dict[str, list[FakeResponse]]) -> None:
        self.by_task, self.urls = by_task, []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get(self, url: str, headers=None) -> FakeResponse:
        self.urls.append(url)
        task_id = url.rsplit("id=", 1)[-1]
        responses = self.by_task[task_id]
        return responses.pop(0) if len(responses) > 1 else responses[0]


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Mock()
        self.store.job.return_value = {"status": "running"}

    def _run(self, by_task: dict[str, list[FakeResponse]], task_ids: list[str]) -> dict:
        client = FakeClient(by_task)
        binding = {
            "base_url": "https://open.mxapi.org",
            "query_path": "/api/v2/music/task?id={task_id}",
            "timeout_seconds": 60,
            "auth_type": "none",
        }
        with patch("control_plane.audio_generation_tasks.httpx.Client", return_value=client), patch(
            "control_plane.audio_generation_tasks.time.sleep"
        ), patch(
            "control_plane.audio_generation_tasks._persist_audio_results",
            side_effect=lambda songs, request, job_id: [f"https://oss/{s['clip_id']}.mp3" for s in songs],
        ) as persist:
            result = _poll_songs(self.store, "job-1", {}, binding, task_ids, 0.0)
        self.client, self.persist = client, persist
        return result

    def test_two_tasks_become_two_songs_in_task_order(self) -> None:
        self._run(
            {"139830437": [FakeResponse(SIBLING_A)], "139830438": [FakeResponse(SIBLING_B)]},
            ["139830437", "139830438"],
        )

        songs = self.persist.call_args.args[0]
        self.assertEqual([s["clip_id"][:8] for s in songs], ["46595f83", "9f4c2e50"])
        self.assertIn("https://open.mxapi.org/api/v2/music/task?id=139830438", self.client.urls)
        final = self.store.update_job.call_args_list[-1].kwargs
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(
            json.loads(final["result_urls_json"]),
            [
                "https://oss/46595f83-e1d3-481c-932c-a7307e9eb1d1.mp3",
                "https://oss/9f4c2e50-40ec-4d47-a25d-4b5e8ce63e95.mp3",
            ],
        )
        self.assertEqual(len(json.loads(final["upstream_response_json"])["songs"]), 2)

    def test_a_failed_sibling_does_not_discard_the_song_that_finished(self) -> None:
        self._run(
            {"139830437": [FakeResponse(SIBLING_A)], "139830438": [FakeResponse(FAILED)]},
            ["139830437", "139830438"],
        )

        final = self.store.update_job.call_args_list[-1].kwargs
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(len(json.loads(final["result_urls_json"])), 1)
        errors = json.loads(final["upstream_response_json"])["task_errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("139830438", errors[0])

    def test_no_song_at_all_fails_the_job(self) -> None:
        self._run({"139830437": [FakeResponse(FAILED)], "139830438": [FakeResponse(FAILED)]}, ["139830437", "139830438"])

        final = self.store.update_job.call_args_list[-1].kwargs
        self.assertEqual(final["status"], "failed")
        self.assertIn("139830437", final["error"])
        self.persist.assert_not_called()


class ObservabilityRouteTests(unittest.TestCase):
    def test_routes_are_observable(self) -> None:
        create = route_info("POST", "/v1/audio-generations/jobs")
        status_info = route_info("GET", "/v1/audio-generations/jobs/2f1c5a44-0000-4000-8000-000000000000")

        self.assertEqual(create.service, "audio_generation")
        self.assertTrue(create.creates_task)
        self.assertTrue(status_info.status_query)
        self.assertTrue(route_info("POST", "/v1/audio-generations/jobs/2f1c5a44-0000-4000-8000-000000000000/cancel").cancel)


class ValidationTests(unittest.TestCase):
    def _reject(self, **kwargs) -> str:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(_validate_audio_generation_request(AudioGenerationRequest(**kwargs)))
        self.assertEqual(caught.exception.status_code, 422)
        return caught.exception.detail

    def test_music_needs_prompt_or_lyrics(self) -> None:
        self.assertIn("prompt or lyrics", self._reject(model="suno-v6"))

    def test_modes_are_mutually_exclusive(self) -> None:
        self.assertIn(
            "mutually exclusive",
            self._reject(model="suno-v6", prompt="一首歌", lyrics="[Verse]\n词"),
        )

    def test_instrumental_cannot_carry_lyrics(self) -> None:
        self.assertIn(
            "instrumental",
            self._reject(model="suno-v6", lyrics="[Verse]\n词", instrumental=True),
        )

    def test_sound_needs_a_title(self) -> None:
        self.assertIn("title", self._reject(model="suno-sound", tags="rain"))


if __name__ == "__main__":
    unittest.main()
