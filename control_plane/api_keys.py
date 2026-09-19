from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


UTC = timezone.utc


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class ApiKeyStore:
    """Persistent API keys. Plaintext keys are returned once and never stored."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

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

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
            "quota": row["quota"], "used": row["used"],
            "remaining": None if row["quota"] is None else max(0, row["quota"] - row["used"]),
            "expires_at": row["expires_at"], "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
        }

    def create(self, name: str, quota: int | None, expires_at: str | None) -> dict[str, Any]:
        plaintext = "aic_" + secrets.token_urlsafe(32)
        prefix = plaintext[:12]
        identifier = str(uuid4())
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO api_keys(id,name,key_prefix,key_hash,quota,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (identifier, name.strip(), prefix, self._hash(plaintext), quota, expires_at, _now()),
            )
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (identifier,)).fetchone()
        result = self._public(row)
        result["api_key"] = plaintext
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [self._public(row) for row in rows]

    def update(self, identifier: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"name", "quota", "expires_at", "enabled"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if "name" in values:
            values["name"] = str(values["name"]).strip()
        if not values:
            with self._connect() as db:
                row = db.execute("SELECT * FROM api_keys WHERE id=?", (identifier,)).fetchone()
            return self._public(row) if row else None
        fields = ",".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE api_keys SET {fields} WHERE id=?", (*values.values(), identifier))
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (identifier,)).fetchone()
        return self._public(row) if row else None

    def delete(self, identifier: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM api_keys WHERE id=?", (identifier,))
        return cursor.rowcount > 0

    def authenticate_and_consume(self, plaintext: str) -> tuple[bool, str]:
        digest = self._hash(plaintext)
        now = _now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM api_keys WHERE key_hash=?", (digest,)).fetchone()
            if not row:
                return False, "invalid"
            if not row["enabled"]:
                return False, "disabled"
            if row["expires_at"] and row["expires_at"] <= now:
                return False, "expired"
            if row["quota"] is not None and row["used"] >= row["quota"]:
                return False, "quota_exceeded"
            db.execute("UPDATE api_keys SET used=used+1,last_used_at=? WHERE id=?", (now, row["id"]))
        return True, "ok"
