from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")


class CreditQuotaExceeded(Exception):
    """这个 Key 今天剩下的积分不够这一单了。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _today_start() -> str:
    """北京时间当天零点，换算成与 _now() 同一口径的 UTC 字符串。"""
    midnight = datetime.combine(datetime.now(SHANGHAI).date(), time.min, SHANGHAI)
    return midnight.astimezone(UTC).isoformat(timespec="milliseconds")


class ApiKeyStore:
    """Persistent API keys. Plaintext keys are returned once and never stored."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """一次操作的连接：成功提交、出错回滚、无论如何关闭。

        不用 `with sqlite3.connect(...)`，那个只做提交不关闭连接，句柄会留在
        进程里（Windows 上还会让临时库文件删不掉）。
        """
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    quota INTEGER,
                    used INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS api_keys_prefix_idx ON api_keys(key_prefix)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(api_keys)")}
            if "daily_credit_quota" not in columns:
                db.execute("ALTER TABLE api_keys ADD COLUMN daily_credit_quota INTEGER")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS api_key_credit_charges (
                    job_id TEXT PRIMARY KEY,
                    key_id TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS api_key_credit_day_idx ON api_key_credit_charges(key_id, created_at)"
            )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _credit_used(db: sqlite3.Connection, key_id: str) -> int:
        row = db.execute(
            """SELECT COALESCE(SUM(credits),0) FROM api_key_credit_charges
               WHERE key_id=? AND state!='refunded' AND created_at>=?""",
            (key_id, _today_start()),
        ).fetchone()
        return int(row[0] or 0)

    @classmethod
    def _public(cls, row: sqlite3.Row, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        quota = row["daily_credit_quota"] if "daily_credit_quota" in row.keys() else None
        result = {
            "id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
            "quota": row["quota"], "used": row["used"],
            "remaining": None if row["quota"] is None else max(0, row["quota"] - row["used"]),
            "daily_credit_quota": quota,
            "credit_used_today": None,
            "expires_at": row["expires_at"], "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
        }
        if db is not None:
            result["credit_used_today"] = cls._credit_used(db, row["id"])
        return result

    def create(self, name: str, quota: int | None, expires_at: str | None,
               daily_credit_quota: int | None = None) -> dict[str, Any]:
        plaintext = "aic_" + secrets.token_urlsafe(32)
        prefix = plaintext[:12]
        identifier = str(uuid4())
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO api_keys(id,name,key_prefix,key_hash,quota,daily_credit_quota,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (identifier, name.strip(), prefix, self._hash(plaintext), quota, daily_credit_quota, expires_at, _now()),
            )
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (identifier,)).fetchone()
            result = self._public(row, db)
        result["api_key"] = plaintext
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
            return [self._public(row, db) for row in rows]

    def update(self, identifier: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"name", "quota", "expires_at", "enabled", "daily_credit_quota"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if "name" in values:
            values["name"] = str(values["name"]).strip()
        with self._lock, self._connect() as db:
            if values:
                fields = ",".join(f"{key}=?" for key in values)
                db.execute(f"UPDATE api_keys SET {fields} WHERE id=?", (*values.values(), identifier))
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (identifier,)).fetchone()
            return self._public(row, db) if row else None

    def delete(self, identifier: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM api_keys WHERE id=?", (identifier,))
        return cursor.rowcount > 0

    def authenticate_and_consume(self, plaintext: str) -> tuple[bool, str, dict[str, Any] | None]:
        digest = self._hash(plaintext)
        now = _now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM api_keys WHERE key_hash=?", (digest,)).fetchone()
            if not row:
                return False, "invalid", None
            if not row["enabled"]:
                return False, "disabled", None
            if row["expires_at"] and row["expires_at"] <= now:
                return False, "expired", None
            if row["quota"] is not None and row["used"] >= row["quota"]:
                return False, "quota_exceeded", None
            db.execute("UPDATE api_keys SET used=used+1,last_used_at=? WHERE id=?", (now, row["id"]))
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (row["id"],)).fetchone()
            return True, "ok", self._public(row, db)

    def charge(self, key_id: str, job_id: str, credits: int) -> dict[str, Any]:
        """占用一单的积分额度。超额抛 CreditQuotaExceeded，此时不会留下记录。

        预扣而不是事后结算：预扣才能在上游真正花钱之前挡住；作业失败或取消时
        由 settle() 退掉，所以当天真正的消耗只包含还没结束的和成功的。
        """
        if credits <= 0:
            return {"charged": 0, "used": self.usage(key_id)}
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT daily_credit_quota FROM api_keys WHERE id=?", (key_id,)).fetchone()
            if row is None:
                return {"charged": 0, "used": 0}
            quota = row["daily_credit_quota"]
            used = self._credit_used(db, key_id)
            if quota is not None and used + credits > quota:
                raise CreditQuotaExceeded(
                    f"daily credit quota exceeded: used {used} + this job {credits} > quota {quota} credits"
                )
            stamp = _now()
            db.execute(
                """INSERT OR REPLACE INTO api_key_credit_charges(job_id,key_id,credits,state,created_at,updated_at)
                   VALUES(?,?,?,'held',?,?)""",
                (job_id, key_id, credits, stamp, stamp),
            )
            return {"charged": credits, "used": used + credits, "quota": quota}

    def settle(self, job_id: str, status: str) -> None:
        """作业终结时结算：失败/取消退额度，成功落定。重复调用无副作用。"""
        if status not in {"succeeded", "failed", "cancelled"}:
            return
        state = "spent" if status == "succeeded" else "refunded"
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE api_key_credit_charges SET state=?, updated_at=? WHERE job_id=? AND state='held'",
                (state, _now(), job_id),
            )

    def usage(self, key_id: str) -> int:
        with self._connect() as db:
            return self._credit_used(db, key_id)
