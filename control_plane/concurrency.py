"""并发闸门：让「一个模块同时跑几个任务」变成可在控制台热改的参数。

背景：任务级并发以前是 systemd 单元里的 Celery `--concurrency`，不是运行时可变的。
这里的做法是不动 Celery 语义，在任务最外层加一层分布式槽位闸门：
**进程池大小 = 允许的上限，实际并发布由配置值卡住。**
配置值调大对正在等的任务立刻生效；调小不打断在跑的任务（只影响后续获取）。

槽位算法是 ZSET 上的 rank-ticket，刻意不用 Lua：
`rank` 是 ZSET 上的全序，所以「rank < limit 的成员数」恒 ≤ limit —— 精确、无竞态。

**票号必须唯一且单调**，这是踩过的坑：早先版本用 `now + ttl` 当 score，两次领取
落在同一时钟刻度就得到相同的 score，而 ZSET 对同分成员按成员名字典序排——
第二个插入者可能排到 rank 0，第一个却已经通过检查、不会重判，于是 limit=1
也能同时进两个。所以这里把两件事拆成两个键：

- `...:slots:<slot>`（票号）：score = `INCR` 得到的票号，单调递增。后来者永远排在
  后面，所以持有者的 rank 只会因别人离场而前移，**不会因别人到场而后退**。
- `...:slots:<slot>:expiry`（存活）：score = 到期时间，心跳只续这个键，
  不碰票号——排名因此在整个持有期间稳定。

判活用 TTL 心跳（10s 续期 / 60s 过期），进程猝死后槽位最多 60s 被回收。
**已知取舍**：若某进程卡到 60s 没发出心跳，它的槽位会被回收，可能出现短暂超发一位。
这比「进程死了槽位永久卡住」好，代价写明在这里而不是藏起来。
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator

import redis

from .celery_app import celery_app
from .config import Settings, get_settings
from .runtime_settings import RuntimeSettingsStore

SLOT_KEY_PREFIX = "aicentre:concurrency:slots:"
SLOT_SEQ_KEY = "aicentre:concurrency:sequence"
HEARTBEAT_SECONDS = 10.0
SLOT_TTL_SECONDS = 60.0
POLL_SECONDS = 1.0
MIN_LIMIT = 1
MAX_LIMIT = 64
SLOT_WAIT_MARGIN_SECONDS = 60.0


@dataclass(frozen=True)
class ConcurrencyModule:
    """一个模块的两级并发。

    job_default 是**今天的实际有效并发**（不是单元里那个数字）：
    video_depth 的单元写 `--concurrency=2`，但它的 `_gpu_slot` flock 把实际并发钉在 1，
    所以默认值必须是 1，否则这次改动会顺手改变线上行为。
    """

    module: str
    label: str
    queue: str
    service: str
    task: str
    job_default: int
    job_adjustable: bool = True
    job_note: str | None = None
    segment_key: str | None = None
    segment_label: str | None = None
    segment_default: int | None = None
    slot: str | None = None

    @property
    def slot_name(self) -> str:
        return self.slot or self.module


MODULES: tuple[ConcurrencyModule, ...] = (
    ConcurrencyModule(
        module="video_upscale", label="视频超分", queue="video_upscale",
        service="ai-centre-video-upscale-worker.service", task="control_plane.video_upscale",
        job_default=1,
        segment_key="video_upscale.segment_concurrency", segment_label="切片并发", segment_default=3,
    ),
    ConcurrencyModule(
        module="video_review", label="视频审核", queue="video_review",
        service="ai-centre-video-review-worker.service", task="control_plane.video_review",
        job_default=1,
    ),
    ConcurrencyModule(
        module="scene_detect", label="视频切片", queue="scene_detect",
        service="ai-centre-scene-worker.service", task="control_plane.scene_detect",
        job_default=1,
    ),
    ConcurrencyModule(
        module="video_depth", label="视频深度推理", queue="video_depth",
        service="ai-centre-depth-worker.service", task="control_plane.video_depth",
        job_default=1, slot="gpu",
        job_note="与「音频分离」共用同一个 GPU 槽位（原 flock 锁的是同一个文件）；"
                 "默认 1 即维持现状，调大等于放开两者的跨模块互斥",
    ),
    ConcurrencyModule(
        module="video_generation", label="视频生成", queue="video_generation",
        service="ai-centre-video-generation-worker.service", task="control_plane.video_generation",
        job_default=4,
    ),
    ConcurrencyModule(
        module="watermark_remove", label="视频去水印", queue="watermark_remove",
        service="ai-centre-watermark-worker.service", task="control_plane.watermark_remove",
        job_default=1,
    ),
    ConcurrencyModule(
        module="audio_separation", label="音频分离", queue="audio_separation",
        service="ai-centre-audio-separation-worker.service", task="control_plane.audio_separation",
        job_default=1, job_adjustable=False, slot="gpu",
        job_note="Celery 池是 --pool=solo，物理上不可能并行两个任务；"
                 "与「视频深度推理」共用同一个 GPU 槽位",
        segment_key="audio_separation.batch_size", segment_label="推理批大小", segment_default=4,
    ),
    ConcurrencyModule(
        module="color_grade", label="视频调色", queue="color_grade",
        service="ai-centre-color-grade-worker.service", task="control_plane.color_grade",
        job_default=1,
    ),
)

MODULE_INDEX: dict[str, ConcurrencyModule] = {item.module: item for item in MODULES}


class SlotTimeout(RuntimeError):
    pass


def module_registry() -> tuple[ConcurrencyModule, ...]:
    return MODULES


def get_module(module: str) -> ConcurrencyModule:
    try:
        return MODULE_INDEX[module]
    except KeyError as exc:
        raise ValueError(f"unknown concurrency module: {module}") from exc


def job_key(module: str) -> str:
    """这个模块**所用槽位**的并发配置键。

    通常是自己的，但 depth / audio_separation 共用 "gpu" 槽位——
    它们原先是 flock 同一个锁文件，所以必须共用一个上限。
    """
    return f"{get_module(module).slot_name}.job"


def shared_with(module: str) -> list[str]:
    """同一槽位上的其它模块名（用来在控制台里说清楚「你在改的是谁」）。"""
    spec = get_module(module)
    return [item.module for item in MODULES if item.slot_name == spec.slot_name and item.module != spec.module]



def segment_key(module: str) -> str | None:
    return get_module(module).segment_key


def registered_keys() -> set[str]:
    keys: set[str] = set()
    for item in MODULES:
        keys.add(job_key(item.module))
        if item.segment_key:
            keys.add(item.segment_key)
    return keys


def default_for(key: str) -> int | None:
    for item in MODULES:
        if key == job_key(item.module):
            return item.job_default
        if item.segment_key and key == item.segment_key:
            return item.segment_default
    return None


@lru_cache(maxsize=1)
def get_runtime_store() -> RuntimeSettingsStore:
    return RuntimeSettingsStore(get_settings().runtime_settings_db_path)


def runtime_int(key: str, default: int) -> int:
    """读运行期覆盖值；store 出问题就退回默认，不让参数库的故障放大成任务全挂。"""
    try:
        return get_runtime_store().get_int(key, default)
    except Exception:
        return default


def effective_limit(module: str) -> int:
    spec = get_module(module)
    return max(MIN_LIMIT, runtime_int(job_key(module), spec.job_default))


def segment_limit(module: str) -> int:
    spec = get_module(module)
    if not spec.segment_key:
        raise ValueError(f"{module} has no segment level knob")
    return max(MIN_LIMIT, runtime_int(spec.segment_key, spec.segment_default or MIN_LIMIT))


def task_soft_limit(module: str) -> float | None:
    """从 Celery 读任务自己的软超时；读不到就返回 None（不设上限）。

    排队发生在**任务内部**，所以等待吃的是同一个超时预算。默认等待上限 2h
    比多数视频族任务的软超时（7140s）还长：不封顶的话，排到一半会被 Celery
    先杀掉，报一个看不出原因的 SoftTimeLimitExceeded，而不是这里的 SlotTimeout。
    """
    try:
        task = celery_app.tasks.get(get_module(module).task)
    except Exception:
        return None
    limit = getattr(task, "soft_time_limit", None)
    return float(limit) if limit else None


def wait_budget(module: str) -> float:
    configured = configured_wait_seconds()
    soft = task_soft_limit(module)
    if soft is None:
        return configured
    # 留一点余量，让任务来得及把「排队超时」这个原因报出来，而不是被硬杀。
    return max(1.0, min(configured, soft - SLOT_WAIT_MARGIN_SECONDS))


def configured_wait_seconds() -> float:
    """配置的等待上限。设置读不出来时退回字段默认值，不让参数缺失把闸门拖死。"""
    try:
        return float(get_settings().concurrency_slot_wait_seconds)
    except Exception:
        return float(Settings.model_fields["concurrency_slot_wait_seconds"].default)


@lru_cache(maxsize=1)
def _client_cache() -> redis.Redis:
    # 用 broker 库（db 12，worker 协调域）；前缀避开 Celery 自己的键。
    return redis.Redis.from_url(get_settings().redis_broker_url, decode_responses=True)


def _client(client: redis.Redis | None) -> redis.Redis:
    return client if client is not None else _client_cache()


def slot_key(module: str) -> str:
    """票号键（score = 单调票号）。"""
    return f"{SLOT_KEY_PREFIX}{get_module(module).slot_name}"


def slot_expiry_key(module: str) -> str:
    """存活键（score = 到期时间）。心跳只碰这个，票号因此不会把持有者挤后。"""
    return f"{SLOT_KEY_PREFIX}{get_module(module).slot_name}:expiry"


def slot_state(module: str, client: redis.Redis | None = None) -> dict[str, Any]:
    """当前占槽情况，**供显示用**。

    `active` 是个近似值：等待者插入票号、判完 rank 再退出，中间有极短窗口会被算进来
    （表现为偶尔多一位）。要精确判定「是否超发」得在临界区里自己数，别拿这个数当判据。
    """
    connection = _client(client)
    key = slot_key(module)
    _reap(connection, module, time.time())
    return {
        "slot": get_module(module).slot_name,
        "limit": effective_limit(module),
        "active": int(connection.zcard(key)),
        "owners": list(connection.zrange(key, 0, -1)),
    }


def _reap(connection: redis.Redis, module: str, now: float) -> int:
    """回收心跳过期的持有者：存活键上按到期时间挑，再从票号键上摘掉。

    票号键自己不存时间，所以过期只能从存活键反查——这是把排名与判活拆开的代价，
    换来的是持有者的 rank 在整段持有期内不会变。
    """
    stale = connection.zrangebyscore(slot_expiry_key(module), "-inf", now)
    if not stale:
        return 0
    connection.zrem(slot_expiry_key(module), *stale)
    connection.zrem(slot_key(module), *stale)
    return len(stale)


def _claim(
    connection: redis.Redis,
    module: str,
    owner: str,
    limit: int,
    ttl_seconds: float,
    now: float,
) -> bool:
    _reap(connection, module, now)
    tickets = slot_key(module)
    ticket = int(connection.incr(SLOT_SEQ_KEY))
    connection.zadd(tickets, {owner: ticket})
    connection.zadd(slot_expiry_key(module), {owner: now + ttl_seconds})
    rank = connection.zrank(tickets, owner)
    if rank is None:
        # 极端情况：刚插进去就被别人回收了。重试即可。
        return False
    if rank < limit:
        return True
    connection.zrem(tickets, owner)
    connection.zrem(slot_expiry_key(module), owner)
    return False


@contextmanager
def job_slot(
    module: str,
    *,
    client: redis.Redis | None = None,
    on_wait: Callable[[int], None] | None = None,
    wait_seconds: float | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> Iterator[dict[str, Any]]:
    """占一个任务级槽位。拿不到就阻塞等待，期间每轮重读配置值。

    on_wait 只在**第一次**需要等待时回调一次，给调用方上报「在排队」状态
    （每轮都回调会把进度写爆）。
    wait_seconds 显式给出时不封顶；不给则取配置值与任务软超时的较小者（见 wait_budget）。
    """
    connection = _client(client)
    owner = uuid.uuid4().hex
    budget = wait_seconds if wait_seconds is not None else wait_budget(module)
    deadline = time.monotonic() + budget
    limit = effective_limit(module)
    waited = False
    while True:
        limit = effective_limit(module)
        if _claim(connection, module, owner, limit, SLOT_TTL_SECONDS, time.time()):
            break
        if not waited:
            waited = True
            if on_wait is not None:
                on_wait(limit)
        if time.monotonic() >= deadline:
            raise SlotTimeout(
                f"timed out waiting for a {module} concurrency slot "
                f"(limit={limit}, waited={int(budget)}s)"
            )
        time.sleep(poll_seconds)

    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                # 只续存活键。票号不动，所以持有者的 rank 在整段持有期内不会变。
                connection.zadd(
                    slot_expiry_key(module), {owner: time.time() + SLOT_TTL_SECONDS}, xx=True
                )
            except Exception:
                # 心跳失败不致命：这一轮由 TTL 兜底，主流程结束时还会 zrem。
                continue

    beat = threading.Thread(target=_beat, name=f"slot-heartbeat-{module}", daemon=True)
    beat.start()
    try:
        yield {"module": module, "owner": owner, "limit": limit, "waited": waited, "budget": budget}
    finally:
        stop.set()
        try:
            connection.zrem(slot_key(module), owner)
            connection.zrem(slot_expiry_key(module), owner)
        except Exception:
            # 释放失败也要让任务本身的结果不被这个异常盖掉；槽位由 TTL 回收。
            pass


def slot_wait_reporter(task: Any) -> Callable[[int], None]:
    """给 job_slot 的 on_wait 用：把「在排队」如实上报。

    没有这一条，任务卡在闸门上时对外仍然是 running——这正是运维当初无法区分
    「排队」和「卡死」的成因。上报失败不该拖住排队本身，所以吞掉异常。
    """

    def report(limit: int) -> None:
        try:
            task.update_state(
                state="PROGRESS",
                meta={"stage": "waiting_slot", "progress": 0, "slot_limit": limit},
            )
        except Exception:
            pass

    return report


CommandResult = tuple[int, str, str]
def _systemctl(*args: str) -> CommandResult:
    try:
        completed = subprocess.run(
            ["systemctl", *args], capture_output=True, check=False, text=True, timeout=30
        )
    except FileNotFoundError:
        # 开发/测试机（Windows）没有 systemctl。按「读不到单元」处理，让调用方去降级，
        # 而不是把一次环境差异放大成 500。
        return 127, "", "systemctl not available"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


_POOL_PATTERN = re.compile(r"--pool=([A-Za-z]+)")
_CONCURRENCY_PATTERN = re.compile(r"--concurrency[= ](\d+)")


def unit_concurrency(service: str, runner: Callable[..., CommandResult] = _systemctl) -> dict[str, Any]:
    """从单元读回**允许的上限**（池大小）。

    上限不是运行时可改的——那是部署动作。控制台只如实显示它，
    目标值超过上限就标「不会生效」，而不是静默接受一个不生效的数。
    """
    code, stdout, stderr = runner("--user", "show", service, "-p", "ExecStart", "--value")
    if code != 0:
        return {"known": False, "value": None, "pool": None, "error": stderr or f"exit {code}"}
    pool = _POOL_PATTERN.search(stdout)
    concurrency = _CONCURRENCY_PATTERN.search(stdout)
    return {
        "known": concurrency is not None,
        "value": int(concurrency.group(1)) if concurrency else None,
        "pool": pool.group(1) if pool else "prefork",
        "error": None,
    }


def module_state(module: str, client: redis.Redis | None = None, runner: Callable[..., CommandResult] = _systemctl) -> dict[str, Any]:
    spec = get_module(module)
    ceiling = unit_concurrency(spec.service, runner=runner)
    value = max(MIN_LIMIT, runtime_int(job_key(module), spec.job_default))
    try:
        live = slot_state(module, client=client)
    except Exception as exc:  # noqa: BLE001
        # Redis 读不到不该让整页 500：配置值仍然可读可改，只是实时占槽显示不出来。
        # 这与 unit_concurrency 对 systemctl 抖动的处理是同一个态度。
        live = {"slot": spec.slot_name, "limit": value, "active": None, "owners": [], "error": str(exc)}
    state: dict[str, Any] = {
        "module": spec.module,
        "label": spec.label,
        "queue": spec.queue,
        "service": spec.service,
        "task": spec.task,
        "slot": spec.slot_name,
        "shared_with": shared_with(module),
        "job": {
            "key": job_key(module),
            "value": value,
            "default": spec.job_default,
            "adjustable": spec.job_adjustable,
            "note": spec.job_note,
            "ceiling": ceiling["value"],
            "ceiling_known": ceiling["known"],
            "pool": ceiling["pool"],
            "effective": (
                True if not ceiling["known"] else bool(ceiling["value"] and value <= ceiling["value"])
            ),
        },
        "segment": None,
        "wait": {
            "configured": configured_wait_seconds(),
            "effective": wait_budget(module),
            "task_soft_limit": task_soft_limit(module),
        },
        "live": live,
    }
    if spec.segment_key:
        segment_value = max(MIN_LIMIT, runtime_int(spec.segment_key, spec.segment_default or MIN_LIMIT))
        state["segment"] = {
            "key": spec.segment_key,
            "label": spec.segment_label,
            "value": segment_value,
            "default": spec.segment_default,
            "adjustable": True,
            "effective": True,
        }
    return state


def all_module_states(client: redis.Redis | None = None, runner: Callable[..., CommandResult] = _systemctl) -> dict[str, Any]:
    return {
        "items": [module_state(item.module, client=client, runner=runner) for item in MODULES],
        "min": MIN_LIMIT,
        "max": MAX_LIMIT,
    }
