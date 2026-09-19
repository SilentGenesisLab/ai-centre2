from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .h3_metrics import infer_machine_type, mark_iqr_outliers, metric_summary


TERMINAL = {"succeeded", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def masked_worker_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or "worker"
    visible = host[:4]
    return urlunsplit((parts.scheme, f"{visible}***", "", "", ""))


def scrub_url_query(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def elapsed_seconds_since(value: str | None) -> float | None:
    if not value:
        return None
    try:
        started_at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds()), 3)


class H3JobNotFound(KeyError):
    pass


class H3WorkerNotFound(KeyError):
    pass


class H3Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    draining INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    queue_running INTEGER NOT NULL DEFAULT 0,
                    queue_pending INTEGER NOT NULL DEFAULT 0,
                    gpu_name TEXT,
                    vram_total_bytes INTEGER,
                    vram_free_bytes INTEGER,
                    current_job_id TEXT,
                    last_error TEXT,
                    last_checked_at TEXT,
                    offline_since TEXT,
                    status_since TEXT,
                    last_assigned_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    external_ref TEXT,
                    priority INTEGER NOT NULL DEFAULT 500,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    active_attempt INTEGER NOT NULL DEFAULT 0,
                    current_worker_id TEXT,
                    source_duration_seconds REAL,
                    result_url TEXT,
                    part_urls_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    elapsed_seconds REAL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    lease_version TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt_ids_json TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(job_id, attempt_number),
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
                    FOREIGN KEY(worker_id) REFERENCES workers(id)
                );
                CREATE INDEX IF NOT EXISTS idx_h3_jobs_status ON jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_h3_attempts_job ON attempts(job_id, attempt_number);
                CREATE TABLE IF NOT EXISTS pool_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    min_workers INTEGER NOT NULL DEFAULT 1,
                    max_workers INTEGER NOT NULL DEFAULT 5,
                    idle_timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                    default_machine_type TEXT NOT NULL DEFAULT '5090_32g',
                    provider_mode TEXT NOT NULL DEFAULT 'spot',
                    spot_estimated_exec_seconds INTEGER NOT NULL DEFAULT 82800,
                    autoscaling_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_deployments (
                    id TEXT PRIMARY KEY,
                    provider_task_id INTEGER UNIQUE,
                    worker_id TEXT,
                    name TEXT NOT NULL,
                    machine_type TEXT NOT NULL,
                    provider_mode TEXT NOT NULL DEFAULT 'deployment',
                    status TEXT NOT NULL,
                    worker_url TEXT,
                    idle_since TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(id)
                );
                CREATE TABLE IF NOT EXISTS pool_incidents (
                    id TEXT PRIMARY KEY,
                    machine_type TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    capacity_at_failure INTEGER NOT NULL DEFAULT 0,
                    first_failure_at TEXT NOT NULL,
                    last_failure_at TEXT NOT NULL,
                    next_retry_at TEXT NOT NULL,
                    alerted_at TEXT,
                    resolved_at TEXT,
                    notification_status TEXT,
                    notification_error TEXT,
                    notification_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_h3_pool_incidents_active
                    ON pool_incidents(machine_type,error_code,resolved_at,last_failure_at);
                CREATE TABLE IF NOT EXISTS job_stage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'estimated',
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_h3_job_stage_events
                    ON job_stage_events(job_id,occurred_at);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "output_json" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN output_json TEXT")
            if "priority" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 500")
            worker_columns = {row[1] for row in connection.execute("PRAGMA table_info(workers)")}
            if "deleted_at" not in worker_columns:
                connection.execute("ALTER TABLE workers ADD COLUMN deleted_at TEXT")
            if "offline_since" not in worker_columns:
                connection.execute("ALTER TABLE workers ADD COLUMN offline_since TEXT")
            if "status_since" not in worker_columns:
                connection.execute("ALTER TABLE workers ADD COLUMN status_since TEXT")
                connection.execute(
                    """UPDATE workers SET status_since=CASE
                         WHEN status='offline' THEN COALESCE(offline_since,created_at)
                         WHEN status='busy' THEN COALESCE(last_assigned_at,created_at)
                         WHEN status='online' THEN COALESCE(
                           (SELECT MAX(a.finished_at) FROM attempts a
                            WHERE a.worker_id=workers.id AND a.finished_at IS NOT NULL),
                           created_at
                         )
                         ELSE COALESCE(updated_at,created_at)
                       END
                       WHERE status_since IS NULL"""
                )
            pool_columns = {row[1] for row in connection.execute("PRAGMA table_info(pool_config)")}
            if "provider_mode" not in pool_columns:
                connection.execute("ALTER TABLE pool_config ADD COLUMN provider_mode TEXT NOT NULL DEFAULT 'spot'")
            if "spot_estimated_exec_seconds" not in pool_columns:
                connection.execute(
                    "ALTER TABLE pool_config ADD COLUMN spot_estimated_exec_seconds INTEGER NOT NULL DEFAULT 82800"
                )
            deployment_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(managed_deployments)")
            }
            if "provider_mode" not in deployment_columns:
                connection.execute(
                    "ALTER TABLE managed_deployments ADD COLUMN provider_mode TEXT NOT NULL DEFAULT 'deployment'"
                )
            stage_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(job_stage_events)")
            }
            if "source" not in stage_columns:
                # Existing transitions predate explicit phase instrumentation.
                connection.execute(
                    "ALTER TABLE job_stage_events ADD COLUMN source TEXT NOT NULL DEFAULT 'estimated'"
                )
            connection.execute(
                "INSERT OR IGNORE INTO pool_config(id,updated_at) VALUES(1,?)", (utc_now(),)
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_h3_jobs_queue "
                "ON jobs(status, priority DESC, created_at ASC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_h3_jobs_analytics ON jobs(created_at,status)"
            )
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def create_worker(self, name: str, base_url: str, enabled: bool = True) -> dict[str, Any]:
        worker_id = str(uuid4())
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workers(
                     id,name,base_url,enabled,status_since,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (worker_id, name, base_url.rstrip("/"), int(enabled), now, now, now),
            )
        return self.worker(worker_id, public=True)

    def pool_config(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = dict(connection.execute("SELECT * FROM pool_config WHERE id=1").fetchone())
        row["autoscaling_enabled"] = bool(row["autoscaling_enabled"])
        return row

    def update_pool_config(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "min_workers", "max_workers", "idle_timeout_seconds", "default_machine_type",
            "provider_mode", "spot_estimated_exec_seconds", "autoscaling_enabled",
        }
        updates = {key: value for key, value in values.items() if key in allowed and value is not None}
        if "autoscaling_enabled" in updates:
            updates["autoscaling_enabled"] = int(bool(updates["autoscaling_enabled"]))
        updates["updated_at"] = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE pool_config SET {','.join(f'{key}=?' for key in updates)} WHERE id=1",
                list(updates.values()),
            )
        return self.pool_config()

    def request_managed_worker(
        self,
        machine_type: str,
        *,
        provider_mode: str = "spot",
        name: str | None = None,
    ) -> dict[str, Any]:
        deployment_id = str(uuid4())
        now = utc_now()
        worker_name = name or f"h3-auto-{machine_type}-{deployment_id[:8]}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO managed_deployments(
                   id,name,machine_type,provider_mode,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (deployment_id, worker_name, machine_type, provider_mode, "requested", now, now),
            )
        return self.managed_deployment(deployment_id)

    def adopt_managed_worker(self, provider_task_id: int, name: str, machine_type: str, worker_url: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM managed_deployments WHERE provider_task_id=?", (provider_task_id,)
            ).fetchone()
            worker_row = connection.execute(
                "SELECT id FROM workers WHERE base_url=? AND deleted_at IS NULL", (worker_url.rstrip("/"),)
            ).fetchone()
        if existing:
            return self.managed_deployment(str(existing[0]))
        worker = self.worker(str(worker_row[0]), public=False) if worker_row else self.create_worker(name, worker_url, True)
        deployment_id = str(uuid4())
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO managed_deployments(id,provider_task_id,worker_id,name,machine_type,status,worker_url,
                   created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?,?)""",
                (deployment_id, provider_task_id, worker["id"], name, machine_type, worker_url.rstrip("/"), now, now),
            )
        return self.managed_deployment(deployment_id)

    def managed_deployment(self, deployment_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = self._row(connection.execute("SELECT * FROM managed_deployments WHERE id=?", (deployment_id,)).fetchone())
        if not row:
            raise KeyError(deployment_id)
        return row

    def list_managed_deployments(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE status NOT IN ('stopped','failed')" if active_only else ""
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM managed_deployments {where} ORDER BY created_at DESC"
            )]

    def active_pool_incidents(self, machine_type: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE resolved_at IS NULL"
        params: list[Any] = []
        if machine_type:
            where += " AND machine_type=?"
            params.append(machine_type)
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM pool_incidents {where} ORDER BY last_failure_at DESC",
                params,
            )]

    def provisioning_allowed(self, machine_type: str, *, now: datetime | None = None) -> tuple[bool, str | None]:
        current = now or datetime.now(timezone.utc)
        incidents = self.active_pool_incidents(machine_type)
        blocked_until = max(
            (datetime.fromisoformat(str(item["next_retry_at"])) for item in incidents),
            default=None,
        )
        if blocked_until and blocked_until > current:
            return False, blocked_until.isoformat()
        return True, None

    def record_pool_failure(
        self,
        machine_type: str,
        error_code: str,
        error: str,
        capacity_at_failure: int,
        *,
        balance_cooldown_seconds: int,
        other_backoff_max_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        current_iso = current.isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM pool_incidents
                   WHERE machine_type=? AND error_code=? AND resolved_at IS NULL
                   ORDER BY last_failure_at DESC LIMIT 1""",
                (machine_type, error_code),
            ).fetchone()
            count = int(row["consecutive_failures"]) + 1 if row else 1
            delay = (
                int(balance_cooldown_seconds)
                if error_code == "C004"
                else min(60 * (2 ** (count - 1)), int(other_backoff_max_seconds))
            )
            next_retry_at = (current + timedelta(seconds=delay)).isoformat()
            if row:
                incident_id = str(row["id"])
                connection.execute(
                    """UPDATE pool_incidents SET last_error=?,consecutive_failures=?,
                       last_failure_at=?,next_retry_at=? WHERE id=?""",
                    (error[:500], count, current_iso, next_retry_at, incident_id),
                )
            else:
                incident_id = str(uuid4())
                connection.execute(
                    """INSERT INTO pool_incidents(
                       id,machine_type,error_code,last_error,consecutive_failures,
                       capacity_at_failure,first_failure_at,last_failure_at,next_retry_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        incident_id, machine_type, error_code, error[:500], count,
                        int(capacity_at_failure), current_iso, current_iso, next_retry_at,
                    ),
                )
        return next(
            item for item in self.active_pool_incidents(machine_type)
            if item["id"] == incident_id
        )

    def record_pool_notification(
        self,
        incident_id: str,
        *,
        status: str,
        error: str | None = None,
        alerted: bool = False,
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE pool_incidents SET notification_status=?,notification_error=?,
                   notification_at=?,alerted_at=CASE WHEN ? THEN COALESCE(alerted_at,?) ELSE alerted_at END
                   WHERE id=?""",
                (status, error[:500] if error else None, now, int(alerted), now, incident_id),
            )

    def resolve_pool_incident(self, incident_id: str, *, notification_status: str) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE pool_incidents SET resolved_at=?,notification_status=?,
                   notification_at=?,notification_error=NULL WHERE id=? AND resolved_at IS NULL""",
                (now, notification_status, now, incident_id),
            )

    def update_managed_deployment(self, deployment_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"provider_task_id", "worker_id", "status", "worker_url", "idle_since", "last_error"}
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["updated_at"] = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE managed_deployments SET {','.join(f'{key}=?' for key in updates)} WHERE id=?",
                [*updates.values(), deployment_id],
            )
        return self.managed_deployment(deployment_id)

    def pending_job_count(self) -> int:
        with self._lock, self._connect() as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {self._pending_queue_where()}"
            ).fetchone()[0])

    def worker(self, worker_id: str, *, public: bool = False) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = self._row(connection.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone())
        if not row:
            raise H3WorkerNotFound(worker_id)
        if public:
            row["base_url"] = masked_worker_url(str(row["base_url"]))
        row["enabled"] = bool(row["enabled"])
        row["draining"] = bool(row["draining"])
        row["offline_duration_seconds"] = elapsed_seconds_since(row.get("offline_since"))
        row["status_duration_seconds"] = elapsed_seconds_since(row.get("status_since"))
        return row

    def list_workers(self, *, public: bool = True) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM workers WHERE deleted_at IS NULL ORDER BY name")]
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["draining"] = bool(row["draining"])
            row["offline_duration_seconds"] = elapsed_seconds_since(row.get("offline_since"))
            row["status_duration_seconds"] = elapsed_seconds_since(row.get("status_since"))
            if public:
                row["base_url"] = masked_worker_url(str(row["base_url"]))
        return rows

    def worker_summary(self) -> dict[str, Any]:
        workers = self.list_workers(public=False)
        summary = {
            "total": len(workers),
            "online": 0,
            "idle": 0,
            "busy": 0,
            "offline": 0,
            "disabled": 0,
            "draining": 0,
            "incompatible": 0,
            "unknown": 0,
        }
        for worker in workers:
            if not worker["enabled"]:
                summary["disabled"] += 1
                continue
            status = str(worker["status"])
            reachable = status in {"online", "busy"} and not worker["draining"]
            if reachable:
                summary["online"] += 1
            if worker["draining"]:
                summary["draining"] += 1
            if status == "incompatible":
                summary["incompatible"] += 1
            elif status == "offline":
                summary["offline"] += 1
            elif status == "busy" or worker["current_job_id"] or worker["queue_running"] or worker["queue_pending"]:
                summary["busy"] += 1
            elif status == "online" and not worker["draining"]:
                summary["idle"] += 1
            elif not reachable:
                summary["unknown"] += 1
        summary["generated_at"] = utc_now()
        return summary

    def update_worker(self, worker_id: str, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "base_url", "enabled", "draining"}
        updates = {key: value for key, value in values.items() if key in allowed and value is not None}
        if not updates:
            return self.worker(worker_id, public=True)
        if "base_url" in updates:
            updates["base_url"] = str(updates["base_url"]).rstrip("/")
        if "enabled" in updates:
            updates["enabled"] = int(bool(updates["enabled"]))
        if "draining" in updates:
            updates["draining"] = int(bool(updates["draining"]))
        updates["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE workers SET {assignments} WHERE id=?",
                [*updates.values(), worker_id],
            )
            if not cursor.rowcount:
                raise H3WorkerNotFound(worker_id)
        return self.worker(worker_id, public=True)

    def delete_worker(self, worker_id: str) -> None:
        with self._lock, self._connect() as connection:
            active = connection.execute(
                "SELECT current_job_id FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
            if not active:
                raise H3WorkerNotFound(worker_id)
            if active[0]:
                raise ValueError("busy worker cannot be deleted")
            now = utc_now()
            connection.execute(
                """UPDATE workers SET enabled=0,draining=0,status='deleted',status_since=?,deleted_at=?,updated_at=?,
                   name=name||'__deleted__'||substr(id,1,8),base_url='https://deleted.invalid' WHERE id=?""",
                (now, now, now, worker_id),
            )

    def record_probe(
        self,
        worker_id: str,
        *,
        ok: bool,
        compatible: bool = True,
        queue_running: int = 0,
        queue_pending: int = 0,
        gpu_name: str | None = None,
        vram_total_bytes: int | None = None,
        vram_free_bytes: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT failure_count,success_count,enabled,current_job_id,status,
                          offline_since,status_since FROM workers WHERE id=?""",
                (worker_id,),
            ).fetchone()
            if not row:
                raise H3WorkerNotFound(worker_id)
            failures = 0 if ok else int(row["failure_count"]) + 1
            successes = int(row["success_count"]) + 1 if ok else 0
            if not compatible:
                status = "incompatible"
            elif not row["enabled"]:
                status = "disabled"
            elif not ok and failures >= 3:
                status = "offline"
            elif ok and successes >= 2:
                status = "busy" if row["current_job_id"] or queue_running or queue_pending else "online"
            else:
                status = connection.execute("SELECT status FROM workers WHERE id=?", (worker_id,)).fetchone()[0]
            offline_since = (row["offline_since"] or now) if status == "offline" else None
            status_since = (row["status_since"] or now) if status == row["status"] else now
            connection.execute(
                """UPDATE workers SET status=?,failure_count=?,success_count=?,queue_running=?,queue_pending=?,
                   gpu_name=COALESCE(?,gpu_name),vram_total_bytes=COALESCE(?,vram_total_bytes),
                   vram_free_bytes=COALESCE(?,vram_free_bytes),last_error=?,last_checked_at=?,offline_since=?,
                   status_since=?,updated_at=? WHERE id=?""",
                (status, failures, successes, queue_running, queue_pending, gpu_name,
                 vram_total_bytes, vram_free_bytes, error, now, offline_since, status_since, now, worker_id),
            )
        return self.worker(worker_id, public=True)

    def cleanup_offline_workers(self, max_age_seconds: int) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM workers
                   WHERE deleted_at IS NULL AND status='offline' AND offline_since IS NOT NULL
                     AND offline_since<=? AND current_job_id IS NULL
                     AND id NOT IN (
                       SELECT worker_id FROM managed_deployments
                       WHERE worker_id IS NOT NULL AND status NOT IN ('stopped','failed')
                     )""",
                (cutoff,),
            ).fetchall()
        worker_ids = [str(row[0]) for row in rows]
        for worker_id in worker_ids:
            self.delete_worker(worker_id)
        return worker_ids

    def schedulable_workers(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM workers WHERE enabled=1 AND draining=0 AND deleted_at IS NULL AND status='online'
                   AND current_job_id IS NULL AND queue_running=0 AND queue_pending=0
                   ORDER BY COALESCE(last_assigned_at,'') ASC, name ASC"""
            )]

    def create_job(self, job_id: str, request: dict[str, Any], priority: int = 500) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO jobs(id,request_json,external_ref,priority,status,stage,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, json.dumps(request, ensure_ascii=False), request.get("external_ref"),
                 priority, "queued", "queued", now, now),
            )
            connection.execute(
                "INSERT INTO job_stage_events(job_id,stage,occurred_at,source) VALUES(?,?,?,'measured')",
                (job_id, "queued", now),
            )

    @staticmethod
    def _pending_queue_where() -> str:
        return (
            "status IN ('queued','running') AND current_worker_id IS NULL "
            "AND stage NOT IN ('cancel_requested','cancelled','failed','completed')"
        )

    def queue_position(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id,status,stage,priority,created_at,current_worker_id FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise H3JobNotFound(job_id)
            queued = bool(
                row["current_worker_id"] is None
                and row["status"] in {"queued", "running"}
                and row["stage"] not in {"cancel_requested", "cancelled", "failed", "completed"}
            )
            if not queued:
                return {
                    "job_id": job_id,
                    "status": row["status"],
                    "stage": row["stage"],
                    "priority": row["priority"],
                    "queued": False,
                    "ahead_count": 0,
                    "position": None,
                    "queued_total": connection.execute(
                        f"SELECT COUNT(*) FROM jobs WHERE {self._pending_queue_where()}"
                    ).fetchone()[0],
                }
            ahead = connection.execute(
                f"""SELECT COUNT(*) FROM jobs WHERE {self._pending_queue_where()} AND
                    (priority>? OR (priority=? AND (created_at<? OR (created_at=? AND id<?))))""",
                (row["priority"], row["priority"], row["created_at"], row["created_at"], job_id),
            ).fetchone()[0]
            total = connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {self._pending_queue_where()}"
            ).fetchone()[0]
        return {
            "job_id": job_id,
            "status": row["status"],
            "stage": row["stage"],
            "priority": row["priority"],
            "queued": True,
            "ahead_count": ahead,
            "position": ahead + 1,
            "queued_total": total,
        }

    def is_next_dispatch_job(self, job_id: str) -> bool:
        position = self.queue_position(job_id)
        return bool(position["queued"] and position["ahead_count"] == 0)

    def request(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT request_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise H3JobNotFound(job_id)
        return json.loads(row[0])

    def scrub_request_urls(self, job_id: str) -> None:
        request = self.request(job_id)
        for key in (
            "reference_video_url", "reference_image_url", "identity_image_url",
            "reference_audio_url",
        ):
            if request.get(key):
                request[key] = scrub_url_query(str(request[key]))
        for key in ("reference_video_urls", "reference_image_urls", "reference_audio_urls"):
            if request.get(key):
                request[key] = [scrub_url_query(str(value)) for value in request[key]]
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET request_json=?,updated_at=? WHERE id=?",
                (json.dumps(request, ensure_ascii=False), utc_now(), job_id),
            )

    def update_job(self, job_id: str, **values: Any) -> None:
        allowed = {
            "status", "stage", "progress", "attempt_count", "active_attempt",
            "current_worker_id", "source_duration_seconds", "result_url", "error",
            "started_at", "finished_at", "elapsed_seconds",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if "part_urls" in values:
            updates["part_urls_json"] = json.dumps(values["part_urls"], ensure_ascii=False)
        if "output_metadata" in values:
            updates["output_json"] = json.dumps(values["output_metadata"], ensure_ascii=False)
        updates["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?", [*updates.values(), job_id]
            )
            if not cursor.rowcount:
                raise H3JobNotFound(job_id)
            if values.get("stage"):
                connection.execute(
                    "INSERT INTO job_stage_events(job_id,stage,occurred_at,source) VALUES(?,?,?,'measured')",
                    (job_id, str(values["stage"]), updates["updated_at"]),
                )

    def begin_attempt(self, job_id: str, worker_id: str, attempt_number: int, lease_version: str) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO attempts(job_id,attempt_number,lease_version,worker_id,status,started_at)
                   VALUES(?,?,?,?,?,?)""",
                (job_id, attempt_number, lease_version, worker_id, "running", now),
            )
            connection.execute(
                """UPDATE jobs SET status='running',stage='uploading_to_worker',progress=20,
                   attempt_count=?,active_attempt=?,current_worker_id=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?""",
                (attempt_number, attempt_number, worker_id, now, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_stage_events(job_id,stage,occurred_at,source) VALUES(?,?,?,'measured')",
                (job_id, "uploading_to_worker", now),
            )
            connection.execute(
                """UPDATE workers SET status='busy',status_since=?,current_job_id=?,
                   last_assigned_at=?,updated_at=? WHERE id=?""",
                (now, job_id, now, now, worker_id),
            )

    def set_attempt_prompt_ids(self, job_id: str, attempt_number: int, prompt_ids: list[str]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE attempts SET prompt_ids_json=? WHERE job_id=? AND attempt_number=?",
                (json.dumps(prompt_ids), job_id, attempt_number),
            )

    def finish_attempt(self, job_id: str, attempt_number: int, status: str, error: str | None = None) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT worker_id FROM attempts WHERE job_id=? AND attempt_number=?",
                (job_id, attempt_number),
            ).fetchone()
            connection.execute(
                "UPDATE attempts SET status=?,error=?,finished_at=? WHERE job_id=? AND attempt_number=?",
                (status, error, now, job_id, attempt_number),
            )
            if row:
                connection.execute(
                    """UPDATE jobs SET current_worker_id=NULL,updated_at=?
                       WHERE id=? AND active_attempt=? AND current_worker_id=?""",
                    (now, job_id, attempt_number, row["worker_id"]),
                )
                connection.execute(
                    """UPDATE workers SET current_job_id=NULL,
                       status=CASE WHEN enabled=0 THEN 'disabled' WHEN status='incompatible' THEN status
                                   WHEN ?='succeeded' THEN 'online' ELSE 'unknown' END,
                       status_since=CASE
                         WHEN status=(CASE WHEN enabled=0 THEN 'disabled'
                                           WHEN status='incompatible' THEN status
                                           WHEN ?='succeeded' THEN 'online' ELSE 'unknown' END)
                         THEN COALESCE(status_since,?) ELSE ? END,
                       success_count=CASE WHEN ?='succeeded' THEN success_count ELSE 0 END,
                       updated_at=? WHERE id=? AND current_job_id=?""",
                    (status, status, now, now, status, now, row["worker_id"], job_id),
                )

    def complete_job_if_active(
        self,
        job_id: str,
        attempt_number: int,
        lease_version: str,
        *,
        result_url: str,
        part_urls: list[str],
        elapsed_seconds: float,
        output_metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT 1 FROM jobs j JOIN attempts a ON a.job_id=j.id AND a.attempt_number=j.active_attempt
                   WHERE j.id=? AND j.active_attempt=? AND a.lease_version=? AND a.status='running'
                   AND j.status NOT IN ('cancel_requested','cancelled','succeeded')""",
                (job_id, attempt_number, lease_version),
            ).fetchone()
            if not active:
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                "UPDATE attempts SET status='succeeded',finished_at=? WHERE job_id=? AND attempt_number=?",
                (now, job_id, attempt_number),
            )
            worker = connection.execute(
                "SELECT worker_id FROM attempts WHERE job_id=? AND attempt_number=?",
                (job_id, attempt_number),
            ).fetchone()
            connection.execute(
                """UPDATE jobs SET status='succeeded',stage='completed',progress=100,result_url=?,
                   part_urls_json=?,output_json=?,error=NULL,finished_at=?,elapsed_seconds=?,updated_at=? WHERE id=?""",
                (result_url, json.dumps(part_urls, ensure_ascii=False),
                 json.dumps(output_metadata, ensure_ascii=False) if output_metadata else None,
                 now, elapsed_seconds, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_stage_events(job_id,stage,occurred_at,source) VALUES(?,?,?,'measured')",
                (job_id, "completed", now),
            )
            if worker:
                connection.execute(
                    """UPDATE workers SET current_job_id=NULL,
                       status=CASE WHEN enabled=0 THEN 'disabled' WHEN status='incompatible' THEN status ELSE 'online' END,
                       status_since=CASE
                         WHEN status=(CASE WHEN enabled=0 THEN 'disabled'
                                           WHEN status='incompatible' THEN status ELSE 'online' END)
                         THEN COALESCE(status_since,?) ELSE ? END,
                       updated_at=? WHERE id=? AND current_job_id=?""",
                    (now, now, now, worker["worker_id"], job_id),
                )
            return True

    def attempt_runtime(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = self._row(connection.execute(
                """SELECT a.*,w.base_url FROM attempts a JOIN workers w ON w.id=a.worker_id
                   WHERE a.job_id=? AND a.status='running' ORDER BY a.attempt_number DESC LIMIT 1""",
                (job_id,),
            ).fetchone())
        if row and row.get("prompt_ids_json"):
            row["prompt_ids"] = json.loads(row["prompt_ids_json"])
        return row

    def job(self, job_id: str, *, include_attempts: bool = True, include_request: bool = False) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = self._row(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            attempts = [dict(item) for item in connection.execute(
                """SELECT attempt_number,status,error,started_at,finished_at
                   FROM attempts WHERE job_id=? ORDER BY attempt_number""", (job_id,)
            )] if include_attempts else []
            events = [dict(item) for item in connection.execute(
                """SELECT stage,occurred_at,source FROM job_stage_events
                   WHERE job_id=? ORDER BY occurred_at,id""",
                (job_id,),
            )] if include_attempts else []
        if not row:
            raise H3JobNotFound(job_id)
        request_json = row.pop("request_json", None)
        if include_request and request_json:
            request = json.loads(request_json)
            for key in ("reference_video_url", "reference_image_url", "identity_image_url", "reference_audio_url"):
                if request.get(key): request[key] = scrub_url_query(str(request[key]))
            for key in ("reference_video_urls", "reference_image_urls", "reference_audio_urls"):
                if request.get(key): request[key] = [scrub_url_query(str(value)) for value in request[key]]
            row["request"] = request
        row["job_id"] = row["id"]
        row["part_urls"] = json.loads(row.pop("part_urls_json") or "[]")
        row["output"] = json.loads(row.pop("output_json") or "null")
        row["attempts"] = attempts
        if include_attempts:
            created = datetime.fromisoformat(str(row["created_at"]))
            finished = datetime.fromisoformat(str(row["finished_at"])) if row.get("finished_at") else None
            successful = next((item for item in reversed(attempts) if item["status"] == "succeeded"), None)
            first_started = datetime.fromisoformat(str(attempts[0]["started_at"])) if attempts else None
            effective = None
            if successful and successful.get("finished_at"):
                effective = max(0.0, (
                    datetime.fromisoformat(str(successful["finished_at"]))
                    - datetime.fromisoformat(str(successful["started_at"]))
                ).total_seconds())
            stage_seconds: dict[str, float] = {}
            for current, following in zip(events, events[1:]):
                seconds = max(0.0, (
                    datetime.fromisoformat(str(following["occurred_at"]))
                    - datetime.fromisoformat(str(current["occurred_at"]))
                ).total_seconds())
                stage = str(current["stage"])
                stage_seconds[stage] = stage_seconds.get(stage, 0.0) + seconds
            row["timing"] = {
                "queue_wait_seconds": round((first_started - created).total_seconds(), 3) if first_started else None,
                "effective_processing_seconds": round(effective, 3) if effective is not None else None,
                "render_and_upload_seconds": round(sum(
                    stage_seconds.get(stage, 0.0)
                    for stage in ("rendering_delivery", "merging", "uploading_oss")
                ), 3) if events else None,
                "end_to_end_seconds": round((finished - created).total_seconds(), 3) if finished else None,
                "stage_seconds": {key: round(value, 3) for key, value in stage_seconds.items()},
            }
        row.pop("current_worker_id", None)
        row.pop("active_attempt", None)
        return row

    def performance_summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            records = [dict(row) for row in connection.execute(
                """SELECT j.id,j.created_at,j.finished_at,j.request_json,j.output_json,d.machine_type,
                          w.gpu_name,w.vram_total_bytes,
                          a.started_at AS attempt_started,a.finished_at AS attempt_finished,
                          (SELECT MIN(started_at) FROM attempts first WHERE first.job_id=j.id) AS first_attempt_at
                   FROM jobs j JOIN attempts a ON a.job_id=j.id
                   LEFT JOIN workers w ON w.id=a.worker_id
                   LEFT JOIN managed_deployments d ON d.worker_id=a.worker_id
                   WHERE j.status='succeeded' AND a.status='succeeded' AND a.finished_at IS NOT NULL"""
            )]
        rows: list[dict[str, Any]] = []
        for record in records:
            request = json.loads(record["request_json"] or "{}")
            output = json.loads(record["output_json"] or "{}")
            started = datetime.fromisoformat(str(record["attempt_started"]))
            finished = datetime.fromisoformat(str(record["attempt_finished"]))
            created = datetime.fromisoformat(str(record["created_at"]))
            first_attempt = datetime.fromisoformat(str(record["first_attempt_at"]))
            image_count = len(request.get("reference_image_urls") or [])
            video_count = len(request.get("reference_video_urls") or [])
            audio_count = len(request.get("reference_audio_urls") or [])
            modes = [name for name, count in (
                ("image", image_count), ("video", video_count), ("audio", audio_count)
            ) if count]
            input_mode = "text" if not modes else modes[0] if len(modes) == 1 else "multimodal"
            duration = output.get("duration_seconds") or request.get("duration_seconds")
            rows.append({
                "job_id": record["id"],
                "resolution": output.get("resolution") or request.get("resolution") or "480p",
                "duration_seconds": float(duration) if duration is not None else None,
                "input_mode": input_mode,
                "kind": request.get("segment_mode") or "single",
                "machine_type": infer_machine_type(
                    record.get("machine_type"), record.get("gpu_name"), record.get("vram_total_bytes")
                ),
                "effective_seconds": max(0.0, (finished - started).total_seconds()),
                "queue_wait_seconds": max(0.0, (first_attempt - created).total_seconds()),
                "end_to_end_seconds": max(0.0, (
                    datetime.fromisoformat(str(record["finished_at"])) - created
                ).total_seconds()) if record.get("finished_at") else None,
            })
        mark_iqr_outliers(
            rows,
            value_key="effective_seconds",
            bucket_keys=("resolution", "duration_seconds", "input_mode", "kind"),
        )
        cleaned = [row for row in rows if not row["effective_outlier"]]
        machine_types = sorted({str(row["machine_type"]) for row in rows})
        return {
            "effective_seconds": metric_summary(row["effective_seconds"] for row in cleaned),
            "raw_effective_seconds": metric_summary(row["effective_seconds"] for row in rows),
            "queue_wait_seconds": metric_summary(row["queue_wait_seconds"] for row in rows),
            "end_to_end_seconds": metric_summary(row["end_to_end_seconds"] for row in rows),
            "sample_count": len(rows),
            "effective_sample_count": len(cleaned),
            "outlier_count": len(rows) - len(cleaned),
            "by_machine_type": {
                machine_type: {
                    "effective_seconds": metric_summary(
                        row["effective_seconds"] for row in cleaned
                        if row["machine_type"] == machine_type
                    ),
                    "success_count": sum(
                        row["machine_type"] == machine_type for row in rows
                    ),
                    "outlier_count": sum(
                        row["machine_type"] == machine_type and row["effective_outlier"]
                        for row in rows
                    ),
                }
                for machine_type in machine_types
            },
        }

    @staticmethod
    def _normalized_stage(stage: str) -> str | None:
        if stage == "downloading":
            return "asset_download"
        if stage == "waiting_for_worker":
            return "queue_wait"
        if stage == "uploading_to_worker":
            return "worker_upload"
        if stage.startswith("generating_part_"):
            return "generation"
        if stage == "rendering_delivery":
            return "delivery_transcode"
        if stage == "merging":
            return "merge"
        if stage == "uploading_oss":
            return "oss_upload"
        if stage == "retrying_on_another_worker":
            return "retry_wait"
        return None

    def duration_analytics(
        self,
        start: str,
        end: str,
        *,
        machine_type: str | None = None,
        resolution: str | None = None,
        input_mode: str | None = None,
        segment_mode: str | None = None,
        duration_min: float = 4,
        duration_max: float = 15,
    ) -> dict[str, Any]:
        """Return raw production H3 timing metrics without outlier removal."""
        with self._lock, self._connect() as connection:
            records = [dict(row) for row in connection.execute(
                """SELECT j.*,
                          (SELECT a.worker_id FROM attempts a WHERE a.job_id=j.id
                           ORDER BY CASE WHEN a.status='succeeded' THEN 0 ELSE 1 END,
                                    a.attempt_number DESC LIMIT 1) AS analytics_worker_id,
                          (SELECT d.machine_type FROM managed_deployments d
                           WHERE d.worker_id=(SELECT a2.worker_id FROM attempts a2 WHERE a2.job_id=j.id
                           ORDER BY CASE WHEN a2.status='succeeded' THEN 0 ELSE 1 END,
                                    a2.attempt_number DESC LIMIT 1) LIMIT 1) AS managed_machine_type,
                          (SELECT w.gpu_name FROM workers w WHERE w.id=(SELECT a3.worker_id
                           FROM attempts a3 WHERE a3.job_id=j.id
                           ORDER BY CASE WHEN a3.status='succeeded' THEN 0 ELSE 1 END,
                                    a3.attempt_number DESC LIMIT 1)) AS gpu_name,
                          (SELECT w.vram_total_bytes FROM workers w WHERE w.id=(SELECT a4.worker_id
                           FROM attempts a4 WHERE a4.job_id=j.id
                           ORDER BY CASE WHEN a4.status='succeeded' THEN 0 ELSE 1 END,
                                    a4.attempt_number DESC LIMIT 1)) AS vram_total_bytes
                   FROM jobs j WHERE j.created_at>=? AND j.created_at<?
                   ORDER BY j.created_at""",
                (start, end),
            )]
            event_rows = [dict(row) for row in connection.execute(
                """SELECT e.job_id,e.stage,e.occurred_at,e.source
                   FROM job_stage_events e JOIN jobs j ON j.id=e.job_id
                   WHERE j.created_at>=? AND j.created_at<?
                   ORDER BY e.job_id,e.occurred_at,e.id""",
                (start, end),
            )]
            attempt_rows = [dict(row) for row in connection.execute(
                """SELECT a.job_id,a.attempt_number,a.status,a.started_at,a.finished_at
                   FROM attempts a JOIN jobs j ON j.id=a.job_id
                   WHERE j.created_at>=? AND j.created_at<?
                   ORDER BY a.job_id,a.attempt_number""",
                (start, end),
            )]

        events_by_job: dict[str, list[dict[str, Any]]] = {}
        for event in event_rows:
            events_by_job.setdefault(str(event["job_id"]), []).append(event)
        attempts_by_job: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempt_rows:
            attempts_by_job.setdefault(str(attempt["job_id"]), []).append(attempt)

        rows: list[dict[str, Any]] = []
        for record in records:
            request = json.loads(record.get("request_json") or "{}")
            output = json.loads(record.get("output_json") or "{}")
            requested_duration = request.get("duration_seconds")
            if requested_duration is None:
                requested_duration = output.get("duration_seconds") or record.get("source_duration_seconds")
            if requested_duration is None:
                continue
            requested_duration = float(requested_duration)
            if not duration_min <= requested_duration <= duration_max:
                continue
            selected_resolution = str(output.get("resolution") or request.get("resolution") or "480p").lower()
            selected_segment = str(request.get("segment_mode") or "single")
            image_count = len(request.get("reference_image_urls") or [])
            video_count = len(request.get("reference_video_urls") or [])
            audio_count = len(request.get("reference_audio_urls") or [])
            modes = [name for name, count in (("image", image_count), ("video", video_count), ("audio", audio_count)) if count]
            selected_input = "text" if not modes else modes[0] if len(modes) == 1 else "multimodal"
            selected_machine = infer_machine_type(
                record.get("managed_machine_type"), record.get("gpu_name"), record.get("vram_total_bytes")
            )
            if machine_type and selected_machine != machine_type:
                continue
            if resolution and selected_resolution != resolution.lower():
                continue
            if input_mode and selected_input != input_mode:
                continue
            if segment_mode and selected_segment != segment_mode:
                continue

            stage_seconds: dict[str, float] = {}
            events = events_by_job.get(str(record["id"]), [])
            for current, following in zip(events, events[1:]):
                normalized = self._normalized_stage(str(current["stage"]))
                if not normalized:
                    continue
                seconds = max(0.0, (
                    datetime.fromisoformat(str(following["occurred_at"]))
                    - datetime.fromisoformat(str(current["occurred_at"]))
                ).total_seconds())
                stage_seconds[normalized] = stage_seconds.get(normalized, 0.0) + seconds
            created = datetime.fromisoformat(str(record["created_at"]))
            finished = datetime.fromisoformat(str(record["finished_at"])) if record.get("finished_at") else None
            end_to_end = max(0.0, (finished - created).total_seconds()) if finished else None
            source = "measured" if events and all(event.get("source") == "measured" for event in events) else "estimated"
            if not stage_seconds:
                attempts = attempts_by_job.get(str(record["id"]), [])
                first_attempt = attempts[0] if attempts else None
                successful_attempt = next((item for item in reversed(attempts) if item["status"] == "succeeded"), None)
                if first_attempt:
                    stage_seconds["queue_wait"] = max(0.0, (
                        datetime.fromisoformat(str(first_attempt["started_at"])) - created
                    ).total_seconds())
                if successful_attempt and successful_attempt.get("finished_at"):
                    stage_seconds["generation"] = max(0.0, (
                        datetime.fromisoformat(str(successful_attempt["finished_at"]))
                        - datetime.fromisoformat(str(successful_attempt["started_at"]))
                    ).total_seconds())
                failed_runtime = sum(max(0.0, (
                    datetime.fromisoformat(str(item["finished_at"]))
                    - datetime.fromisoformat(str(item["started_at"]))
                ).total_seconds()) for item in attempts if item["status"] == "failed" and item.get("finished_at"))
                if failed_runtime:
                    stage_seconds["retry_wait"] = failed_runtime
            rows.append({
                "job_id": record["id"],
                "status": record["status"],
                "duration_bucket": int(round(requested_duration)),
                "requested_duration_seconds": requested_duration,
                "output_duration_seconds": float(output.get("duration_seconds") or requested_duration),
                "resolution": selected_resolution,
                "aspect_ratio": str(output.get("aspect_ratio") or request.get("aspect_ratio") or "9:16"),
                "input_mode": selected_input,
                "segment_mode": selected_segment,
                "machine_type": selected_machine,
                "worker_id": record.get("analytics_worker_id"),
                "attempt_count": int(record.get("attempt_count") or 0),
                "source": source,
                "end_to_end_seconds": end_to_end,
                "stage_seconds": stage_seconds,
            })

        succeeded = [row for row in rows if row["status"] == "succeeded" and row["end_to_end_seconds"] is not None]
        terminal = [row for row in rows if row["status"] in TERMINAL]
        stage_names = [
            "asset_download", "queue_wait", "worker_upload", "generation",
            "delivery_transcode", "merge", "oss_upload", "retry_wait",
        ]

        def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "end_to_end": metric_summary(row["end_to_end_seconds"] for row in items),
                "requested_duration": metric_summary(row["requested_duration_seconds"] for row in items),
                "output_duration": metric_summary(row["output_duration_seconds"] for row in items),
                "stages": {
                    stage: metric_summary(row["stage_seconds"].get(stage) for row in items)
                    for stage in stage_names
                },
            }

        duration_rows = []
        for duration in range(max(4, int(math.ceil(duration_min))), min(15, int(math.floor(duration_max))) + 1):
            items = [row for row in succeeded if row["duration_bucket"] == duration]
            duration_rows.append({"duration_seconds": duration, **summarize(items)})
        machines = sorted({str(row["machine_type"]) for row in rows})
        total_processing_seconds = sum(float(row["end_to_end_seconds"] or 0) for row in succeeded)
        output_seconds = sum(float(row["output_duration_seconds"] or 0) for row in succeeded)
        return {
            "range": {"from": start, "to": end, "timezone": "Asia/Shanghai"},
            "filters": {
                "machine_type": machine_type, "resolution": resolution,
                "input_mode": input_mode, "segment_mode": segment_mode,
                "duration_min": duration_min, "duration_max": duration_max,
            },
            "jobs": len(rows),
            "terminal_jobs": len(terminal),
            "succeeded_jobs": len(succeeded),
            "failed_jobs": sum(row["status"] == "failed" for row in terminal),
            "success_rate": len(succeeded) / len(terminal) if terminal else None,
            "retry_jobs": sum(row["attempt_count"] > 1 for row in rows),
            "retry_rate": sum(row["attempt_count"] > 1 for row in rows) / len(rows) if rows else None,
            "generated_seconds_per_processing_hour": (
                output_seconds / (total_processing_seconds / 3600)
                if total_processing_seconds > 0 else None
            ),
            "coverage": {
                "measured": sum(row["source"] == "measured" for row in succeeded),
                "estimated": sum(row["source"] == "estimated" for row in succeeded),
            },
            "overall": summarize(succeeded),
            "durations": duration_rows,
            "by_machine_type": {
                machine: summarize([row for row in succeeded if row["machine_type"] == machine])
                for machine in machines
            },
        }

    def observability_backfill_rows(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT id AS external_task_id,status,stage,created_at,started_at,finished_at
                   FROM jobs WHERE finished_at IS NOT NULL AND status IN ('succeeded','failed','cancelled')"""
            )]

    def list_jobs(self, limit: int = 100, status: str | None = None) -> dict[str, Any]:
        where = "WHERE status=?" if status else ""
        params: list[Any] = [status] if status else []
        with self._lock, self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
            ids = [row[0] for row in connection.execute(
                f"SELECT id FROM jobs {where} ORDER BY created_at DESC LIMIT ?", [*params, limit]
            )]
        return {"items": [self.job(job_id, include_attempts=False) for job_id in ids], "total": total}

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise H3JobNotFound(job_id)
        return row[0] in {"cancel_requested", "cancelled"}

    def cancel_requested(self, job_id: str) -> dict[str, Any]:
        current = self.job(job_id)
        if current["status"] not in TERMINAL:
            self.update_job(job_id, status="cancel_requested", stage="cancel_requested")
        return self.job(job_id)
