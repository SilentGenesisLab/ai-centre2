"""可热改的运行期参数：单表 key/value，进程内 2 秒快照缓存。

为什么需要它：并发这类参数以前只硬编码在 systemd 单元与 config.py 里，改一次要重新部署。
这个 store 让控制面能改、让 worker 能立刻看到。

读写是不同进程（控制面写、worker 读），所以缓存 TTL 就是「改完多久被 worker 看见」——
2 秒是刻意的：够便宜（等待循环里每秒问一次），又短到运维感觉是即时生效。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RuntimeSettingsStore:
    CACHE_TTL_SECONDS = 2.0

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: dict[str, str] | None = None
        self._cache_at = 0.0
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """提交并**显式关闭**连接。

        为什么不直接 `with self._connect()`：sqlite3.Connection 参与循环引用，
        靠 GC 才释放。Linux 上无所谓（unlink 打开的文件不报错），但在 Windows 上
        会把文件一直锁住；而且这个 store 会被 worker 每秒读一次，不该攒句柄。
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._session() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute("PRAGMA optimize")

    def _read(self) -> dict[str, str]:
        # 无锁：各自开连接就够了，锁只保护缓存。
        with self._session() as connection:
            rows = connection.execute("SELECT key,value FROM runtime_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def all(self) -> dict[str, str]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache
            if cached is not None and now - self._cache_at < self.CACHE_TTL_SECONDS:
                return dict(cached)
        values = self._read()
        with self._lock:
            self._cache, self._cache_at = values, now
        return dict(values)

    def get_int(self, key: str, default: int) -> int:
        raw = self.all().get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            # 值被外部写坏时退回默认，而不是让 worker 起不来。
            return default

    def update(self, values: dict[str, Any], allowed: set[str]) -> dict[str, str]:
        accepted = {key: int(value) for key, value in values.items() if key in allowed and value is not None}
        if not accepted:
            return self.all()
        stamp = _utc_now()
        with self._lock, self._session() as connection:
            connection.executemany(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                [(key, str(value), stamp) for key, value in accepted.items()],
            )
            self._cache = None
        return self.all()

    def reset(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return self.all()
        with self._lock, self._session() as connection:
            connection.executemany(
                "DELETE FROM runtime_settings WHERE key=?", [(key,) for key in keys]
            )
            self._cache = None
        return self.all()
