"""外部大模型接口鉴活。

和小节里的 `health_monitor` 分开，是因为两类目标的失败语义不同：

- `health_monitor` 打的是各服务自己的 `GET /health`，只能说明进程活着；
- 这里真的按 OpenAI 兼容协议发一次 `POST /chat/completions`，模型回一句话
  才算成功。网关把「账号池耗尽」这类错误回成 HTTP 403 或 `{"error": ...}`，
  只看状态码不够，还要看响应体。

调度规则（按模型独立计算，一个模型坏了不拖住其它三个）：

    正常每 15 分钟一次
      失败第 1、2 次 -> 1 分钟后重试
      连续第 3 次失败 -> 记故障、发飞书告警，之后回到 15 分钟节奏
      恢复 -> 发飞书恢复通知（只在确实告过警时发）
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import httpx

from .h3_alerts import H3LarkWebhook, safe_alert_error

UTC = timezone.utc

# 与上游 /v1/models 里实际存在的 id 对齐：
# - `gpt-6.1-sol` 才是上游 id（不是 "gpt6.1 sol"）；
# - opus 只有连字符版 `claude-opus-5-5` 能通，带点的 `claude-opus-5.5`
#   虽然在同一份模型列表里，但调用回 403 "All available accounts exhausted"。
DEFAULT_MODELS = ("gemini-4-argon", "gpt-6.1-sol", "grok-4.7", "claude-opus-5-5")

OPERATIONAL = "operational"
DEGRADED = "degraded"
MAJOR_OUTAGE = "major_outage"
UNKNOWN = "unknown"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_models(raw: str | None) -> tuple[str, ...]:
    items = [item.strip() for item in str(raw or "").split(",")]
    parsed = tuple(item for item in items if item)
    return parsed or DEFAULT_MODELS


class LlmProbeStore:
    def __init__(self, path: Path, models: tuple[str, ...] = DEFAULT_MODELS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init(models)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init(self, models: tuple[str, ...]) -> None:
        stamp = now_iso()
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS llm_probe_targets (
              model TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'unknown',
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_checked_at TEXT, next_check_at TEXT,
              last_latency_ms REAL, last_status_code INTEGER, last_error TEXT,
              last_success_at TEXT, last_failure_at TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_probe_checks (
              id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL,
              checked_at TEXT NOT NULL, success INTEGER NOT NULL, latency_ms REAL,
              status_code INTEGER, error TEXT, attempt TEXT NOT NULL DEFAULT 'schedule'
            );
            CREATE INDEX IF NOT EXISTS idx_llm_probe_checks_model_time
              ON llm_probe_checks(model, checked_at);
            CREATE TABLE IF NOT EXISTS llm_probe_incidents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL,
              status TEXT NOT NULL, started_at TEXT NOT NULL, confirmed_at TEXT NOT NULL,
              recovered_at TEXT, failure_summary TEXT, last_notified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_probe_deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, kind TEXT NOT NULL,
              created_at TEXT NOT NULL, success INTEGER NOT NULL, error TEXT
            );
            """)
            # 新模型立刻跑一次；已存在的保留原来的 next_check_at，避免重启后全部提前探测。
            db.executemany(
                "INSERT INTO llm_probe_targets(model,next_check_at) VALUES(?,?) "
                "ON CONFLICT(model) DO NOTHING",
                [(model, stamp) for model in models],
            )

    def targets(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM llm_probe_targets ORDER BY model")]

    def due_models(self) -> list[str]:
        stamp = now_iso()
        with self.connect() as db:
            return [
                row["model"]
                for row in db.execute(
                    "SELECT model FROM llm_probe_targets "
                    "WHERE next_check_at IS NULL OR next_check_at<=? ORDER BY model",
                    (stamp,),
                )
            ]

    def record(
        self,
        model: str,
        *,
        success: bool,
        latency_ms: float | None,
        status_code: int | None = None,
        error: str | None = None,
        attempt: str = "schedule",
        interval_seconds: int = 900,
        retry_seconds: int = 60,
        failure_threshold: int = 3,
    ) -> dict[str, Any] | None:
        checked_at = datetime.now(UTC)
        checked_iso = checked_at.isoformat()
        transition: dict[str, Any] | None = None
        with self.connect() as db:
            db.execute(
                "INSERT INTO llm_probe_checks(model,checked_at,success,latency_ms,status_code,error,attempt) "
                "VALUES(?,?,?,?,?,?,?)",
                (model, checked_iso, int(success), latency_ms, status_code,
                 safe_alert_error(error) if error else None, attempt),
            )
            row = db.execute("SELECT * FROM llm_probe_targets WHERE model=?", (model,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO llm_probe_targets(model,next_check_at) VALUES(?,?)",
                    (model, checked_iso),
                )
                row = db.execute("SELECT * FROM llm_probe_targets WHERE model=?", (model,)).fetchone()

            old = row["status"] or UNKNOWN
            failures = 0 if success else int(row["consecutive_failures"]) + 1

            if success:
                new = OPERATIONAL
                next_at = (checked_at + timedelta(seconds=interval_seconds)).isoformat()
            elif failures >= failure_threshold:
                new = MAJOR_OUTAGE
                # 连续失败达到阈值后不再 1 分钟重试，回到正常节奏继续观察。
                next_at = (checked_at + timedelta(seconds=interval_seconds)).isoformat()
            else:
                new = DEGRADED
                next_at = (checked_at + timedelta(seconds=retry_seconds)).isoformat()

            db.execute(
                "UPDATE llm_probe_targets SET status=?,consecutive_failures=?,last_checked_at=?,"
                "next_check_at=?,last_latency_ms=?,last_status_code=?,last_error=?,"
                "last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,"
                "last_failure_at=CASE WHEN ? THEN last_failure_at ELSE ? END WHERE model=?",
                (new, failures, checked_iso, next_at, latency_ms, status_code,
                 safe_alert_error(error) if error else None,
                 int(success), checked_iso, int(success), checked_iso, model),
            )

            if new == MAJOR_OUTAGE and old != MAJOR_OUTAGE:
                cursor = db.execute(
                    "INSERT INTO llm_probe_incidents(model,status,started_at,confirmed_at,failure_summary) "
                    "VALUES(?,?,?,?,?)",
                    (model, "open", checked_iso, checked_iso, safe_alert_error(error)),
                )
                transition = {
                    "kind": "failure",
                    "incident_id": cursor.lastrowid,
                    "model": model,
                    "failures": failures,
                    "error": error,
                }
            elif success and old == MAJOR_OUTAGE:
                incident = db.execute(
                    "SELECT id,started_at FROM llm_probe_incidents "
                    "WHERE model=? AND status='open' ORDER BY id DESC LIMIT 1",
                    (model,),
                ).fetchone()
                if incident:
                    db.execute(
                        "UPDATE llm_probe_incidents SET status='recovered',recovered_at=? WHERE id=?",
                        (checked_iso, incident["id"]),
                    )
                    transition = {
                        "kind": "recovery",
                        "incident_id": incident["id"],
                        "model": model,
                        "started_at": incident["started_at"],
                    }
        return transition

    def delivery(self, incident_id: int | None, kind: str, success: bool, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO llm_probe_deliveries(incident_id,kind,created_at,success,error) "
                "VALUES(?,?,?,?,?)",
                (incident_id, kind, now_iso(), int(success), safe_alert_error(error) if error else None),
            )
            if incident_id and success:
                db.execute(
                    "UPDATE llm_probe_incidents SET last_notified_at=? WHERE id=?",
                    (now_iso(), incident_id),
                )

    def last_delivery(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT created_at,success,error,kind FROM llm_probe_deliveries ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else {}

    def status(self, days: int = 30) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        items: list[dict[str, Any]] = []
        with self.connect() as db:
            for target in db.execute("SELECT * FROM llm_probe_targets ORDER BY model"):
                checks = list(db.execute(
                    "SELECT * FROM llm_probe_checks WHERE model=? AND checked_at>=? ORDER BY checked_at",
                    (target["model"], since),
                ))
                latencies = [float(c["latency_ms"]) for c in checks if c["latency_ms"] is not None]
                successes = sum(int(c["success"]) for c in checks)
                item = dict(target)
                item.update(
                    sample_count=len(checks),
                    availability=round(successes * 100 / len(checks), 3) if checks else None,
                    p50_ms=_percentile(latencies, 0.5),
                    p95_ms=_percentile(latencies, 0.95),
                    history=[
                        {"at": c["checked_at"], "ok": bool(c["success"])} for c in checks[-96:]
                    ],
                )
                items.append(item)
            incidents = [
                dict(row) for row in db.execute(
                    "SELECT * FROM llm_probe_incidents ORDER BY id DESC LIMIT 50"
                )
            ]
        return {
            "days": days,
            "items": items,
            "incidents": incidents,
            "last_delivery": self.last_delivery(),
            "generated_at": now_iso(),
        }


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * p + 0.999999) - 1))
    return round(ordered[index], 2)


class LlmMonitor:
    def __init__(self, store: LlmProbeStore, settings: Any) -> None:
        self.store, self.settings = store, settings

    def models(self) -> tuple[str, ...]:
        return parse_models(getattr(self.settings, "llm_probe_models", None))

    # ---------- 通知 ----------

    def _notifier(self) -> H3LarkWebhook | None:
        webhook = getattr(self.settings, "llm_probe_webhook_url", None)
        if not webhook:
            return None
        return H3LarkWebhook(SimpleNamespace(
            h3_lark_webhook_url=webhook,
            h3_lark_webhook_secret=getattr(self.settings, "llm_probe_webhook_secret", None),
            h3_lark_timeout_seconds=getattr(self.settings, "llm_probe_webhook_timeout_seconds", 5.0),
        ))

    def _notify(self, transition: dict[str, Any] | None) -> None:
        if not transition:
            return
        notifier = self._notifier()
        if not notifier:
            return
        kind = transition["kind"]
        occurred = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        fields: list[tuple[str, str]] = [("模型", transition["model"])]
        if kind == "failure":
            fields += [
                ("当前状态", f"连续 {transition.get('failures', 3)} 次调用失败"),
                ("接口", str(getattr(self.settings, "llm_probe_base_url", ""))),
                ("发生时间", f"{occurred}（北京时间）"),
                ("事件编号", str(transition.get("incident_id") or "—")),
            ]
            title, template, icon = "🚨 外部模型接口故障", "red", "🚨"
        else:
            started = transition.get("started_at")
            duration = "—"
            if started:
                try:
                    elapsed = datetime.now(UTC) - datetime.fromisoformat(started)
                    duration = _format_duration(elapsed)
                except ValueError:
                    pass
            fields += [
                ("当前状态", "调用已恢复"),
                ("故障持续", duration),
                ("恢复时间", f"{occurred}（北京时间）"),
                ("事件编号", str(transition.get("incident_id") or "—")),
            ]
            title, template, icon = "✅ 外部模型接口恢复", "green", "✅"
        try:
            notifier.send_card(
                title=f"{icon} {title}",
                template=template,
                fields=fields,
                detail=transition.get("error"),
                button_url=getattr(self.settings, "health_monitor_public_admin_url", None),
            )
            self.store.delivery(transition.get("incident_id"), kind, True)
        except Exception as exc:
            self.store.delivery(transition.get("incident_id"), kind, False, str(exc))

    def test_notification(self) -> None:
        notifier = self._notifier()
        if not notifier:
            raise RuntimeError("飞书Webhook未配置")
        notifier.send_card(
            title="🧪 外部模型鉴活通知测试",
            template="blue",
            fields=[
                ("状态", "外部模型鉴活飞书通知配置有效"),
                ("接口", str(getattr(self.settings, "llm_probe_base_url", ""))),
                ("监控模型", "、".join(self.models())),
                ("发送时间", datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S（北京时间）")),
            ],
        )
        self.store.delivery(None, "test", True)

    # ---------- 探测 ----------

    @staticmethod
    def _judge(response: httpx.Response) -> tuple[bool, str | None]:
        if not 200 <= response.status_code < 300:
            return False, f"HTTP {response.status_code}: {response.text[:300]}"
        try:
            body = response.json()
        except Exception:
            return False, f"响应不是JSON: {response.text[:200]}"
        if not isinstance(body, dict):
            return False, "响应结构异常"
        if body.get("error"):
            return False, f"上游错误: {json.dumps(body['error'], ensure_ascii=False)[:300]}"
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return False, "响应里没有 choices"
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        message = message if isinstance(message, dict) else {}
        content = str(message.get("content") or "").strip()
        reasoning = str(message.get("reasoning_content") or "").strip()
        if not content and not reasoning:
            return False, "回复内容为空"
        return True, None

    def _headers(self) -> dict[str, str]:
        key = getattr(self.settings, "llm_probe_api_key", None)
        token = key.get_secret_value() if key else ""
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _probe(self, client: httpx.AsyncClient, model: str) -> tuple[bool, float, int | None, str | None]:
        url = str(self.settings.llm_probe_base_url).rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": str(self.settings.llm_probe_prompt)}],
            "max_tokens": int(self.settings.llm_probe_max_tokens),
        }
        started = time.perf_counter()
        code: int | None = None
        try:
            response = await client.post(url, json=payload, headers=self._headers())
            code = response.status_code
            ok, error = self._judge(response)
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        latency = round((time.perf_counter() - started) * 1000, 2)
        return ok, latency, code, error

    async def _run_models(self, models: list[str] | tuple[str, ...], attempt: str) -> list[dict[str, Any]]:
        timeout = float(getattr(self.settings, "llm_probe_timeout_seconds", 120))
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            results = await asyncio.gather(
                *(self._check_one(client, model, attempt) for model in models)
            )
        return list(results)

    async def _check_one(self, client: httpx.AsyncClient, model: str, attempt: str) -> dict[str, Any]:
        ok, latency, code, error = await self._probe(client, model)
        transition = self.store.record(
            model,
            success=ok,
            latency_ms=latency,
            status_code=code,
            error=error,
            attempt=attempt,
            interval_seconds=int(getattr(self.settings, "llm_probe_interval_seconds", 900)),
            retry_seconds=int(getattr(self.settings, "llm_probe_retry_seconds", 60)),
            failure_threshold=int(getattr(self.settings, "llm_probe_failure_threshold", 3)),
        )
        self._notify(transition)
        return {
            "model": model,
            "success": ok,
            "latency_ms": latency,
            "status_code": code,
            "error": safe_alert_error(error) if error else None,
            "attempt": attempt,
            "transition": transition["kind"] if transition else None,
        }

    async def run(self, model: str | None = None) -> dict[str, Any]:
        models = [model] if model else list(self.models())
        return {"results": await self._run_models(models, "manual")}

    async def run_due(self) -> dict[str, Any]:
        due = self.store.due_models()
        if not due:
            return {"results": []}
        return {"results": await self._run_models(due, "schedule")}

    async def run_forever(self) -> None:
        """调度循环。

        每个模型自己带 `next_check_at`，所以「某一家失败后 1 分钟重试」不会
        影响其它模型的 15 分钟节奏。循环本身只负责把到点的挑出来。
        """
        if not getattr(self.settings, "llm_probe_enabled", True):
            return
        if not getattr(self.settings, "llm_probe_api_key", None):
            return
        await asyncio.sleep(10)
        while True:
            try:
                await self.run_due()
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(30)


def _format_duration(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"
