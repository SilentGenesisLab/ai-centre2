from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from control_plane.celery_app import celery_app
from control_plane.concurrency import (
    MAX_LIMIT,
    MIN_LIMIT,
    SLOT_WAIT_MARGIN_SECONDS,
    SlotTimeout,
    _claim,
    all_module_states,
    effective_limit,
    job_key,
    job_slot,
    module_registry,
    module_state,
    registered_keys,
    segment_key,
    segment_limit,
    shared_with,
    slot_expiry_key,
    slot_key,
    slot_state,
    task_soft_limit,
    unit_concurrency,
    wait_budget,
)
from control_plane.concurrency_api import (
    ConcurrencyConfigRequest,
    ConcurrencyResetRequest,
    reset_config,
    states,
    update_config,
)
from control_plane.config import Settings
from control_plane.runtime_settings import RuntimeSettingsStore


class FakeRedis:
    """只实现闸门用到的那几个 ZSET 命令，够真、够小。

    刻意不引 fakeredis：本仓库的既有做法就是手写 fake（见 tests/test_h3_pool.py）。
    """

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.counters: dict[str, int] = {}

    def incr(self, key: str) -> int:
        value = self.counters.get(key, 0) + 1
        self.counters[key] = value
        return value

    def _ordered(self, key: str) -> list[str]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda pair: (pair[1], pair[0]))
        return [member for member, _ in items]

    def zrangebyscore(self, key: str, low: str, high: float) -> list[str]:
        del low  # 调用方恒传 "-inf"
        return [member for member, score in self.zsets.get(key, {}).items() if score <= float(high)]

    def zremrangebyscore(self, key: str, low: str, high: float) -> int:
        del low
        zset = self.zsets.get(key, {})
        expired = [member for member, score in zset.items() if score <= float(high)]
        for member in expired:
            del zset[member]
        return len(expired)

    def zadd(self, key: str, mapping: dict[str, float], nx: bool = False, xx: bool = False) -> int:
        zset = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if nx and member in zset:
                continue
            if xx and member not in zset:
                continue
            if member not in zset:
                added += 1
            zset[member] = float(score)
        return added

    def zrank(self, key: str, member: str) -> int | None:
        ordered = self._ordered(key)
        return ordered.index(member) if member in ordered else None

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        ordered = self._ordered(key)
        return ordered[start:] if end == -1 else ordered[start : end + 1]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zrem(self, key: str, *members: str) -> int:
        zset = self.zsets.get(key, {})
        return sum(1 for member in members if zset.pop(member, None) is not None)


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        return self.returncode, self.stdout, self.stderr


EXEC_START = (
    "/home/donxu/ai-centre/.venv-control/bin/celery -A control_plane.celery_app:celery_app "
    "worker -Q video_upscale -n video-upscale@%h --concurrency=1 --loglevel=INFO"
)
EXEC_START_THREADS = (
    "/home/donxu/ai-centre/.venv-control/bin/celery -A control_plane.celery_app:celery_app "
    "worker -Q video_depth -n depth@%h --pool=threads --concurrency=2 --prefetch-multiplier=1"
)


class StoreTestCase(unittest.TestCase):
    """把运行期参数库指向临时文件：绝不碰 /home/donxu 那条线上路径。"""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = RuntimeSettingsStore(Path(self.temporary.name) / "runtime.db")
        self.redis = FakeRedis()
        target = patch("control_plane.concurrency.get_runtime_store", return_value=self.store)
        target.start()
        self.addCleanup(target.stop)

    def set_value(self, key: str, value: int) -> None:
        self.store.update({key: value}, registered_keys())


class GateTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        settings = SimpleNamespace(
            concurrency_slot_wait_seconds=5.0,
            redis_broker_url="redis://unused/0",
            runtime_settings_db_path=self.store.path,
        )
        target = patch("control_plane.concurrency.get_settings", return_value=settings)
        target.start()
        self.addCleanup(target.stop)

    def _slot(self, module: str = "video_upscale", **overrides):
        options: dict = {"client": self.redis, "poll_seconds": 0.01, "wait_seconds": 1.0}
        options.update(overrides)
        return job_slot(module, **options)

    def test_second_claim_is_blocked_while_the_first_holds_the_slot(self) -> None:
        with self._slot():
            with self.assertRaises(SlotTimeout):
                with self._slot(wait_seconds=0.3):
                    self.fail("limit=1 时第二个不该拿到槽位")
        self.assertEqual(self.redis.zcard(slot_key("video_upscale")), 0)

    def test_release_lets_the_next_waiter_in(self) -> None:
        with self._slot():
            pass
        with self._slot() as acquired:
            self.assertTrue(acquired["owner"])

    def test_expired_owner_is_reaped_and_does_not_wedge_the_gate(self) -> None:
        # 模拟一个已经死掉的进程：票和存活记录都在，但心跳早已过期。
        self.redis.zadd(slot_key("video_upscale"), {"dead-process": 1.0})
        self.redis.zadd(slot_expiry_key("video_upscale"), {"dead-process": 1.0})
        with self._slot():
            pass
        self.assertNotIn("dead-process", self.redis.zsets[slot_key("video_upscale")])

    def test_identical_claim_timestamps_do_not_over_admit(self) -> None:
        # 回归：早先用 `now + ttl` 当 score，同一时钟刻度上的两次领取分数相同，
        # ZSET 对同分成员按字典序排，第二个插入者可能拿到 rank 0，而第一个不会重判，
        # 于是 limit=1 也能同时进两个。票号唯一且单调之后，这条不再可能。
        now = 1_800_000_000.0
        self.assertTrue(_claim(self.redis, "video_upscale", "first", 1, 60.0, now))
        self.assertFalse(_claim(self.redis, "video_upscale", "second", 1, 60.0, now))
        self.assertEqual(self.redis.zcard(slot_key("video_upscale")), 1)

    def test_keeper_rank_never_moves_back_while_others_claim(self) -> None:
        # 持有者的 rank 只能因别人离场而前移，不能因别人到场而后退——这是不超发的前提。
        now = 1_800_000_000.0
        self.assertTrue(_claim(self.redis, "video_upscale", "keeper", 2, 60.0, now))
        self.assertTrue(_claim(self.redis, "video_upscale", "second", 2, 60.0, now))
        self.assertFalse(_claim(self.redis, "video_upscale", "third", 2, 60.0, now))
        tickets = slot_key("video_upscale")
        self.assertEqual(self.redis.zrank(tickets, "keeper"), 0)
        self.assertEqual(self.redis.zrank(tickets, "second"), 1)

    def test_raising_the_limit_immediately_admits_waiting_tasks(self) -> None:
        # 这是「热生效」的核心：等待循环每轮重读配置值。
        with patch.object(RuntimeSettingsStore, "CACHE_TTL_SECONDS", 0.0):
            admitted = threading.Event()
            errors: list[BaseException] = []

            def waiter() -> None:
                try:
                    with self._slot(wait_seconds=4.0):
                        admitted.set()
                except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
                    errors.append(exc)

            with self._slot():
                thread = threading.Thread(target=waiter)
                thread.start()
                self.assertFalse(admitted.wait(0.3), "limit=1 时不该被放行")
                self.set_value(job_key("video_upscale"), 2)
                self.assertTrue(admitted.wait(3.0), "调大 limit 后等待中的任务应该立刻进")
            thread.join(timeout=5)
            self.assertEqual(errors, [])

    def test_lowering_the_limit_does_not_evict_a_running_task(self) -> None:
        self.set_value(job_key("video_upscale"), 3)
        with self._slot():
            self.set_value(job_key("video_upscale"), 1)
            self.assertEqual(slot_state("video_upscale", client=self.redis)["active"], 1)

    def test_heartbeat_keeps_the_slot_from_expiring(self) -> None:
        with (
            patch("control_plane.concurrency.HEARTBEAT_SECONDS", 0.02),
            patch("control_plane.concurrency.SLOT_TTL_SECONDS", 0.05),
        ):
            with self._slot():
                owner = self.redis._ordered(slot_key("video_upscale"))[0]
                ticket = self.redis.zsets[slot_key("video_upscale")][owner]
                first_expiry = self.redis.zsets[slot_expiry_key("video_upscale")][owner]
                # 睡过原始 TTL：没有心跳的话它早该被判死。
                threading.Event().wait(0.2)
                self.assertGreater(
                    self.redis.zsets[slot_expiry_key("video_upscale")][owner], first_expiry
                )
                # 心跳不动票号——否则持有者的 rank 会因续期而变。
                self.assertEqual(self.redis.zsets[slot_key("video_upscale")][owner], ticket)

    def test_slot_state_reports_limit_and_active(self) -> None:
        self.set_value(job_key("video_upscale"), 3)
        state = slot_state("video_upscale", client=self.redis)
        self.assertEqual(state["limit"], 3)
        self.assertEqual(state["active"], 0)

    def test_owners_are_unique_per_acquisition(self) -> None:
        with self._slot() as first:
            pass
        with self._slot() as second:
            self.assertNotEqual(first["owner"], second["owner"])

    def test_depth_and_audio_separation_share_one_gpu_slot(self) -> None:
        # 这两个原先 flock 的是同一个文件，所以今天它们是跨模块互斥的。
        # 换成各自独立的闸门会**悄悄放开**这个互斥（GPU 并发 1 → 2），所以必须共用槽位。
        self.assertEqual(job_key("video_depth"), "gpu.job")
        self.assertEqual(job_key("audio_separation"), "gpu.job")
        self.assertEqual(slot_key("video_depth"), slot_key("audio_separation"))
        self.assertEqual(shared_with("video_depth"), ["audio_separation"])
        self.assertEqual(shared_with("audio_separation"), ["video_depth"])

    def test_shared_slot_keeps_the_cross_module_exclusion(self) -> None:
        with self._slot("video_depth"):
            with self.assertRaises(SlotTimeout):
                with self._slot("audio_separation", wait_seconds=0.3):
                    self.fail("GPU 槽位被 depth 占着时，音频分离不该能进")

    def test_raising_the_shared_slot_admits_both_modules(self) -> None:
        self.set_value(job_key("video_depth"), 2)
        with self._slot("video_depth"):
            with self._slot("audio_separation"):
                self.assertEqual(slot_state("video_depth", client=self.redis)["active"], 2)

    def test_modules_without_a_shared_slot_get_their_own(self) -> None:
        self.assertEqual(job_key("video_upscale"), "video_upscale.job")
        self.assertEqual(slot_key("video_upscale"), "aicentre:concurrency:slots:video_upscale")
        self.assertEqual(shared_with("video_upscale"), [])

    def test_wait_callback_fires_once(self) -> None:
        seen: list[int] = []
        with self._slot():
            with self.assertRaises(SlotTimeout):
                with self._slot(wait_seconds=0.3, on_wait=seen.append):
                    self.fail("不该拿到槽位")
        self.assertEqual(seen, [1], "on_wait 只该在第一次等待时回调一次")


class RegistryTests(StoreTestCase):
    def test_only_registered_keys_are_accepted(self) -> None:
        self.store.update({job_key("video_upscale"): 2, "not.a.key": 9}, registered_keys())
        values = self.store.all()
        self.assertEqual(values.get(job_key("video_upscale")), "2")
        self.assertNotIn("not.a.key", values)

    def test_defaults_are_todays_effective_concurrency(self) -> None:
        # video_depth 的单元写 --concurrency=2，但 flock 把实际并发钉在 1，所以默认必须是 1，
        # 否则这次改动会顺手改变线上行为。
        self.assertEqual(effective_limit("video_depth"), 1)
        self.assertEqual(effective_limit("video_upscale"), 1)
        self.assertEqual(effective_limit("video_generation"), 4)

    def test_segment_defaults_match_config_field_defaults(self) -> None:
        # 防漂移：注册表里的段级默认值必须等于 config.py 里那个字段的默认值。
        # 比对 model_fields 而不是 Settings() 实例，避免被 .env / 环境变量带偏。
        fields = Settings.model_fields
        self.assertEqual(
            segment_limit("video_upscale"),
            fields["video_upscale_segment_concurrency"].default,
        )
        self.assertEqual(
            segment_limit("audio_separation"),
            fields["audio_separation_batch_size"].default,
        )

    def test_modules_without_a_segment_knob_say_so(self) -> None:
        for module in ("video_review", "scene_detect", "video_depth", "color_grade"):
            self.assertIsNone(segment_key(module), module)
            with self.assertRaises(ValueError):
                segment_limit(module)

    def test_corrupt_value_falls_back_to_default(self) -> None:
        with self.store._session() as connection:
            connection.execute(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (job_key("video_upscale"), "not-a-number", "2026-01-01T00:00:00+00:00"),
            )
        with patch.object(RuntimeSettingsStore, "CACHE_TTL_SECONDS", 0.0):
            self.assertEqual(effective_limit("video_upscale"), 1)

    def test_limit_is_never_below_one(self) -> None:
        self.set_value(job_key("video_upscale"), 0)
        self.assertEqual(effective_limit("video_upscale"), 1)

    def test_reset_restores_defaults(self) -> None:
        self.set_value(job_key("video_upscale"), 4)
        self.store.reset([job_key("video_upscale")])
        self.assertEqual(effective_limit("video_upscale"), 1)


class UnitReaderTests(StoreTestCase):
    def test_parses_prefork_unit(self) -> None:
        result = unit_concurrency(
            "ai-centre-video-upscale-worker.service", runner=FakeRunner(EXEC_START)
        )
        self.assertEqual(result["value"], 1)
        self.assertEqual(result["pool"], "prefork")
        self.assertTrue(result["known"])

    def test_parses_threads_pool(self) -> None:
        result = unit_concurrency(
            "ai-centre-depth-worker.service", runner=FakeRunner(EXEC_START_THREADS)
        )
        self.assertEqual(result["value"], 2)
        self.assertEqual(result["pool"], "threads")

    def test_unreadable_unit_is_reported_not_guessed(self) -> None:
        result = unit_concurrency("nope.service", runner=FakeRunner(returncode=1, stderr="not found"))
        self.assertFalse(result["known"])
        self.assertIsNone(result["value"])
        self.assertEqual(result["error"], "not found")

    def test_target_above_unit_ceiling_is_flagged_ineffective(self) -> None:
        self.set_value(job_key("video_upscale"), 8)
        state = module_state("video_upscale", client=self.redis, runner=FakeRunner(EXEC_START))
        self.assertEqual(state["job"]["value"], 8)
        self.assertEqual(state["job"]["ceiling"], 1)
        self.assertFalse(state["job"]["effective"], "目标值高于单元上限时必须标不生效")

    def test_target_within_ceiling_is_effective(self) -> None:
        self.set_value(job_key("video_upscale"), 4)
        raised_pool = EXEC_START.replace("--concurrency=1", "--concurrency=4")
        state = module_state("video_upscale", client=self.redis, runner=FakeRunner(raised_pool))
        self.assertEqual(state["job"]["ceiling"], 4)
        self.assertTrue(state["job"]["effective"])

    def test_solo_pool_module_is_read_only(self) -> None:
        state = module_state("audio_separation", client=self.redis, runner=FakeRunner(EXEC_START))
        self.assertFalse(state["job"]["adjustable"])
        self.assertIn("--pool=solo", state["job"]["note"])
        self.assertEqual(state["segment"]["key"], "audio_separation.batch_size")
        self.assertEqual(state["segment"]["default"], 4)

    def test_all_states_cover_the_video_family(self) -> None:
        payload = all_module_states(client=self.redis, runner=FakeRunner(EXEC_START))
        modules = {item["module"] for item in payload["items"]}
        self.assertEqual(
            modules,
            {
                "video_upscale",
                "video_review",
                "scene_detect",
                "video_depth",
                "video_generation",
                "audio_generation",
                "watermark_remove",
                "audio_separation",
                "color_grade",
            },
        )
        for item in payload["items"]:
            self.assertEqual(item["live"]["active"], 0, item["module"])
            self.assertTrue(item["service"].startswith("ai-centre-"), item["module"])


class TaskBudgetTests(StoreTestCase):
    """排队发生在任务内部，吃的是任务自己的超时预算，所以上限必须封在预算内。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 任务名要能在 Celery 注册表里解析出来，先按 celery_app.include 的名单导入。
        import control_plane.audio_generation_tasks  # noqa: F401
        import control_plane.audio_separation_tasks  # noqa: F401
        import control_plane.color_grade_tasks  # noqa: F401
        import control_plane.depth_tasks  # noqa: F401
        import control_plane.generation_tasks  # noqa: F401
        import control_plane.scene_tasks  # noqa: F401
        import control_plane.video_review_tasks  # noqa: F401
        import control_plane.video_upscale_tasks  # noqa: F401
        import control_plane.watermark_tasks  # noqa: F401

    def test_every_module_resolves_to_a_registered_task(self) -> None:
        for item in module_registry():
            with self.subTest(module=item.module):
                self.assertIsNotNone(
                    task_soft_limit(item.module), f"{item.task} 没注册，等待上限会失去封顶"
                )

    def test_wait_budget_is_capped_by_the_task_soft_limit(self) -> None:
        # 配置 7200s 比 depth 的软超时还长：不封顶的话排队会先被 Celery 杀掉，
        # 报 SoftTimeLimitExceeded 而不是能说明原因的 SlotTimeout。
        soft = float(celery_app.tasks["control_plane.video_depth"].soft_time_limit)
        settings = SimpleNamespace(concurrency_slot_wait_seconds=7200.0)
        with patch("control_plane.concurrency.get_settings", return_value=settings):
            self.assertEqual(task_soft_limit("video_depth"), soft)
            self.assertAlmostEqual(wait_budget("video_depth"), soft - SLOT_WAIT_MARGIN_SECONDS)
            # 超分的软超时 14340s 比配置值大，就不该被封顶。
            self.assertEqual(wait_budget("video_upscale"), 7200.0)

    def test_job_slot_defaults_to_the_capped_budget(self) -> None:
        settings = SimpleNamespace(concurrency_slot_wait_seconds=7200.0)
        with patch("control_plane.concurrency.get_settings", return_value=settings):
            with self._held() as held:
                self.assertAlmostEqual(
                    held["budget"], 7200.0, msg="超分不该被自己的软超时封顶"
                )

    def _held(self):
        return job_slot("video_upscale", client=self.redis, poll_seconds=0.01)


class ConcurrencyApiTests(unittest.IsolatedAsyncioTestCase):
    """路由层：白名单、取值范围、改完能读回来。

    直接调协程函数，照 tests/test_url_media_api.py 的做法（本仓库不用 TestClient）。
    """

    async def asyncSetUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = RuntimeSettingsStore(Path(self.temporary.name) / "runtime.db")
        settings = SimpleNamespace(
            concurrency_slot_wait_seconds=7200.0,
            redis_broker_url="redis://unused/0",
            runtime_settings_db_path=self.store.path,
        )
        for target in (
            patch("control_plane.concurrency_api.get_runtime_store", return_value=self.store),
            patch("control_plane.concurrency.get_runtime_store", return_value=self.store),
            patch("control_plane.concurrency.get_settings", return_value=settings),
            patch("control_plane.concurrency._client_cache", return_value=FakeRedis()),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(lambda: patch.stopall())

    async def _set(self, values: dict[str, int]) -> dict:
        # 缓存 TTL 是 2s，同一进程内改完要立刻看见，照其他用例的做法把 TTL 归零。
        with patch.object(RuntimeSettingsStore, "CACHE_TTL_SECONDS", 0.0):
            return await update_config(ConcurrencyConfigRequest(values=values))

    async def test_states_lists_every_module(self) -> None:
        payload = await states()
        self.assertEqual(len(payload["items"]), len(module_registry()))
        self.assertEqual((payload["min"], payload["max"]), (MIN_LIMIT, MAX_LIMIT))

    async def test_unregistered_key_is_rejected_not_ignored(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self._set({"not.a.key": 2})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("not.a.key", caught.exception.detail)

    async def test_out_of_range_value_is_rejected(self) -> None:
        for value in (0, MAX_LIMIT + 1):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as caught:
                    await self._set({job_key("video_upscale"): value})
                self.assertEqual(caught.exception.status_code, 422)

    async def test_update_returns_the_new_state(self) -> None:
        payload = await self._set({job_key("video_upscale"): 3})
        item = next(i for i in payload["items"] if i["module"] == "video_upscale")
        self.assertEqual(item["job"]["value"], 3)
        self.assertEqual(item["job"]["key"], "video_upscale.job")

    async def test_reset_restores_defaults(self) -> None:
        with patch.object(RuntimeSettingsStore, "CACHE_TTL_SECONDS", 0.0):
            await self._set({job_key("video_upscale"): 4})
            payload = await reset_config(ConcurrencyResetRequest(keys=[job_key("video_upscale")]))
        item = next(i for i in payload["items"] if i["module"] == "video_upscale")
        self.assertEqual(item["job"]["value"], item["job"]["default"])

    async def test_reset_without_keys_clears_everything(self) -> None:
        with patch.object(RuntimeSettingsStore, "CACHE_TTL_SECONDS", 0.0):
            await self._set({job_key("video_upscale"): 4})
            await reset_config(ConcurrencyResetRequest())
        self.assertEqual(self.store.all(), {})


class UnreadableEnvironmentTests(StoreTestCase):
    def test_missing_systemctl_is_reported_not_raised(self) -> None:
        # 开发机没有 systemctl：要降级成「读不到上限」，不是抛出去把整页打成 500。
        with patch("control_plane.concurrency.subprocess.run", side_effect=FileNotFoundError):
            result = unit_concurrency("ai-centre-video-upscale-worker.service")
        self.assertFalse(result["known"])
        self.assertIsNone(result["value"])
        self.assertEqual(result["error"], "systemctl not available")

    def test_redis_failure_only_blanks_the_live_numbers(self) -> None:
        class DeadRedis:
            def __getattr__(self, name: str):
                raise ConnectionError("redis is down")

        state = module_state("video_upscale", client=DeadRedis(), runner=FakeRunner(EXEC_START))
        self.assertIsNone(state["live"]["active"])
        self.assertIn("redis is down", state["live"]["error"])
        # 配置值本身仍可读——这正是「Redis 挂了也不该看不见并发」的意思。
        self.assertEqual(state["job"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
