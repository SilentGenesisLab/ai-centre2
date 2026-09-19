from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.h3_store import H3Store
from control_plane.h3_workflow import build_prompt_graph, resolve_dimensions, validate_part_durations


class H3WorkflowTests(unittest.TestCase):
    def test_prompt_is_passed_without_modification(self) -> None:
        prompt = "Use <Picture 1> and <Video 1>.  保留  两个空格。\n第二行"
        graph = build_prompt_graph(
            "input.mp4", prompt, 5, 416, 736, 42, "video/test",
            reference_image_name="identity.png", reference_audio_name="voice.mp3",
            reference_has_audio=True,
        )
        self.assertEqual(graph["136"]["inputs"]["prompt"], prompt)
        self.assertEqual(graph["136"]["inputs"]["ref_images.ref_image_0"], ["3", 0])
        self.assertEqual(graph["136"]["inputs"]["ref_video_audios.ref_video_audio_0"], ["2", 1])
        self.assertEqual(graph["136"]["inputs"]["ref_audios.ref_audio_0"], ["4", 0])

    def test_optional_identity_and_audio_are_not_connected(self) -> None:
        graph = build_prompt_graph(
            "input.mp4", "prompt", 5, 416, 736, 42, "video/test",
            reference_image_name=None, reference_audio_name=None, reference_has_audio=False,
        )
        self.assertNotIn("3", graph)
        self.assertNotIn("ref_images.ref_image_0", graph["136"]["inputs"])
        self.assertNotIn("ref_video_audios.ref_video_audio_0", graph["136"]["inputs"])

    def test_multiple_references_are_all_connected_in_order(self) -> None:
        graph = build_prompt_graph(
            None, "prompt", 5, 416, 736, 42, "video/test",
            video_names=["one.mp4", "two.mp4"],
            reference_image_names=["one.png", "two.png"],
            reference_audio_names=["one.mp3", "two.mp3"],
            reference_video_audio_flags=[True, False],
        )
        inputs = graph["136"]["inputs"]
        self.assertIn("ref_videos.ref_video_1", inputs)
        self.assertIn("ref_images.ref_image_1", inputs)
        self.assertIn("ref_audios.ref_audio_1", inputs)
        self.assertIn("ref_video_audios.ref_video_audio_0", inputs)
        self.assertNotIn("ref_video_audios.ref_video_audio_1", inputs)

    def test_image_and_audio_only_graph_has_no_video_nodes(self) -> None:
        graph = build_prompt_graph(
            None, "prompt", 5, 480, 832, 42, "video/test",
            reference_image_name="image.png", reference_audio_name="audio.mp3",
        )
        self.assertNotIn("1", graph)
        self.assertNotIn("2", graph)
        self.assertEqual(graph["136"]["inputs"]["ref_images.ref_image_0"], ["3", 0])
        self.assertEqual(graph["136"]["inputs"]["ref_audios.ref_audio_0"], ["4", 0])

    def test_text_only_graph_has_no_reference_nodes_or_inputs(self) -> None:
        graph = build_prompt_graph(
            None, "一只雄鹰在黑夜中飞行", 5, 480, 832, 42, "video/test",
        )
        self.assertNotIn("1", graph)
        self.assertNotIn("2", graph)
        self.assertNotIn("3", graph)
        self.assertNotIn("4", graph)
        inputs = graph["136"]["inputs"]
        self.assertFalse(any(key.startswith("ref_") and key != "ref_image_size" for key in inputs))
        self.assertEqual(inputs["prompt"], "一只雄鹰在黑夜中飞行")

    def test_resolution_presets_keep_model_inside_safe_budget(self) -> None:
        model_width, model_height, output_width, output_height = resolve_dimensions("1080p", "9:16")
        self.assertEqual((output_width, output_height), (1080, 1920))
        self.assertEqual(model_width % 32, 0)
        self.assertEqual(model_height % 32, 0)
        self.assertLessEqual(model_width * model_height, 1_048_576)

    def test_720p_preset_uses_native_h3_768_class_canvas(self) -> None:
        self.assertEqual(resolve_dimensions("720p", "9:16"), (768, 1344, 768, 1344))
        self.assertEqual(resolve_dimensions("720p", "16:9"), (1344, 768, 1344, 768))

    def test_quality_profiles_route_dimensions_and_sampler(self) -> None:
        low = resolve_dimensions("720p", "9:16", quality="low")
        medium = resolve_dimensions("720p", "9:16", quality="medium")
        self.assertLess(low[0] * low[1], medium[0] * medium[1])
        self.assertEqual(low[2:], medium[2:])
        low_graph = build_prompt_graph(None, "prompt", 5, low[0], low[1], 42, "video/low", quality="low")
        medium_graph = build_prompt_graph(None, "prompt", 5, medium[0], medium[1], 42, "video/medium", quality="medium")
        high_graph = build_prompt_graph(None, "prompt", 5, medium[0], medium[1], 42, "video/high", quality="high")
        self.assertEqual(low_graph["123"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(medium_graph["123"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(high_graph["123"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(high_graph["124"]["inputs"]["steps"], 4)

    def test_duration_validation(self) -> None:
        self.assertEqual(validate_part_durations(5, "single", None), [5])
        self.assertEqual(validate_part_durations(24, "two_part", 12), [12, 12])
        with self.assertRaises(ValueError):
            validate_part_durations(16, "single", None)
        with self.assertRaises(ValueError):
            validate_part_durations(24, "two_part", 1)


class H3StoreTests(unittest.TestCase):
    def test_priority_queue_orders_highest_first_and_fifo_on_ties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            low = "00000000-0000-0000-0000-000000000001"
            high_old = "00000000-0000-0000-0000-000000000002"
            high_new = "00000000-0000-0000-0000-000000000003"
            store.create_job(low, {"prompts": ["low"]}, priority=100)
            store.create_job(high_old, {"prompts": ["high old"]}, priority=900)
            store.create_job(high_new, {"prompts": ["high new"]}, priority=900)

            self.assertEqual(store.queue_position(high_old)["position"], 1)
            self.assertEqual(store.queue_position(high_new)["position"], 2)
            self.assertEqual(store.queue_position(low)["ahead_count"], 2)
            self.assertTrue(store.is_next_dispatch_job(high_old))
            self.assertFalse(store.is_next_dispatch_job(low))

    def test_worker_summary_separates_idle_busy_offline_and_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            idle = store.create_worker("idle", "https://idle.example")
            busy = store.create_worker("busy", "https://busy.example")
            offline = store.create_worker("offline", "https://offline.example")
            disabled = store.create_worker("disabled", "https://disabled.example", enabled=False)
            for worker_id in (idle["id"], busy["id"]):
                store.record_probe(worker_id, ok=True)
                store.record_probe(worker_id, ok=True)
            store.record_probe(busy["id"], ok=True, queue_running=1)
            for _ in range(3):
                store.record_probe(offline["id"], ok=False)

            summary = store.worker_summary()
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["online"], 2)
            self.assertEqual(summary["idle"], 1)
            self.assertEqual(summary["busy"], 1)
            self.assertEqual(summary["offline"], 1)
            self.assertEqual(summary["disabled"], 1)

    def test_public_records_hide_worker_url_and_internal_prompt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            worker = store.create_worker("worker-1", "https://secret-worker.example/path")
            self.assertNotIn("secret-worker.example", worker["base_url"])
            internal = store.list_workers(public=False)[0]
            job_id = "4c39cf01-9893-436e-9378-1be045d98f64"
            store.create_job(job_id, {
                "reference_video_url": "https://cdn.example/a.mp4?signature=secret",
                "segment_mode": "single", "prompts": ["prompt"],
            })
            store.begin_attempt(job_id, internal["id"], 1, "lease")
            store.set_attempt_prompt_ids(job_id, 1, ["internal-prompt-id"])
            public_job = store.job(job_id)
            self.assertNotIn("request_json", public_job)
            self.assertNotIn("prompt_ids_json", str(public_job))
            store.scrub_request_urls(job_id)
            self.assertNotIn("signature=", str(store.request(job_id)))

    def test_stale_lease_cannot_publish_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            worker = store.create_worker("worker-1", "https://worker.example")
            worker_id = store.list_workers(public=False)[0]["id"]
            job_id = "5c39cf01-9893-436e-9378-1be045d98f64"
            store.create_job(job_id, {"reference_video_url": "https://cdn.example/a.mp4", "prompts": ["p"]})
            store.begin_attempt(job_id, worker_id, 1, "new-lease")
            self.assertFalse(store.complete_job_if_active(
                job_id, 1, "old-lease", result_url="https://oss.example/old.mp4",
                part_urls=[], elapsed_seconds=1,
            ))
            self.assertNotEqual(store.job(job_id)["status"], "succeeded")

    def test_worker_offline_and_recovery_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            created = store.create_worker("worker-1", "https://worker.example")
            worker_id = created["id"]
            self.assertNotEqual(store.record_probe(worker_id, ok=False)["status"], "offline")
            self.assertNotEqual(store.record_probe(worker_id, ok=False)["status"], "offline")
            self.assertEqual(store.record_probe(worker_id, ok=False)["status"], "offline")
            self.assertEqual(store.record_probe(worker_id, ok=True)["status"], "offline")
            self.assertEqual(store.record_probe(worker_id, ok=True)["status"], "online")
            self.assertEqual(store.record_probe(worker_id, ok=True, queue_running=1)["status"], "busy")

    def test_worker_status_duration_tracks_online_and_busy_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            created = store.create_worker("duration-worker", "https://duration.example.com")
            worker_id = created["id"]
            store.record_probe(worker_id, ok=True)
            online = store.record_probe(worker_id, ok=True)
            self.assertEqual(online["status"], "online")
            self.assertIsNotNone(online["status_since"])

            five_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            connection = store._connect()
            try:
                connection.execute(
                    "UPDATE workers SET status_since=? WHERE id=?",
                    (five_minutes_ago, worker_id),
                )
                connection.commit()
            finally:
                connection.close()
            online = store.worker(worker_id, public=True)
            self.assertGreaterEqual(online["status_duration_seconds"], 299)

            busy = store.record_probe(worker_id, ok=True, queue_running=1)
            self.assertEqual(busy["status"], "busy")
            self.assertLess(busy["status_duration_seconds"], 2)
            busy_since = busy["status_since"]

            still_busy = store.record_probe(worker_id, ok=True, queue_running=1)
            self.assertEqual(still_busy["status_since"], busy_since)

            back_online = store.record_probe(worker_id, ok=True)
            self.assertEqual(back_online["status"], "online")
            self.assertNotEqual(back_online["status_since"], busy_since)
            self.assertLess(back_online["status_duration_seconds"], 2)

    def test_failed_attempt_requires_health_recovery_before_rescheduling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = H3Store(Path(temporary) / "h3.db")
            created = store.create_worker("worker-1", "https://worker.example")
            worker_id = created["id"]
            job_id = "6c39cf01-9893-436e-9378-1be045d98f64"
            store.create_job(job_id, {"reference_image_url": "https://cdn.example/a.png", "prompts": ["p"]})
            store.record_probe(worker_id, ok=True)
            store.record_probe(worker_id, ok=True)
            store.begin_attempt(job_id, worker_id, 1, "lease")
            store.finish_attempt(job_id, 1, "failed", "worker unavailable")
            self.assertEqual(store.worker(worker_id)["status"], "unknown")
            self.assertEqual(store.schedulable_workers(), [])
            self.assertTrue(store.queue_position(job_id)["queued"])


if __name__ == "__main__":
    unittest.main()
