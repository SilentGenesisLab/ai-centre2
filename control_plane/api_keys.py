from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .credit_pricing import MICRO, Quote, seed_rates, to_yuan

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")

WINDOWS = {"5h": timedelta(hours=5), "24h": timedelta(hours=24), "total": None}
LIMIT_COLUMNS = {"5h": "limit_5h_micro", "24h": "limit_24h_micro", "total": "limit_total_micro"}
SETTING_KEYS = {
    "platform_limit_5h_micro",
    "platform_limit_24h_micro",
    "platform_limit_total_micro",
    "credits_per_yuan",
}


class CreditQuotaExceeded(Exception):
    """这个 Key（或整个平台）剩下的额度不够这一单了。

    `reason` 指出是哪一条线拦住的，调用方据此报 429 并让人一眼看出该调哪个数。
    """

    def __init__(self, message: str, reason: str = "limit"):
        super().__init__(message)
        self.reason = reason


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _since(window: str) -> str:
    delta = WINDOWS[window]
    if delta is None:
        return ""
    return (datetime.now(UTC) - delta).isoformat(timespec="milliseconds")


def _yuan(amount_micro: int) -> str:
    # 别用「¥」：U+00A5 在 GBK 控制台上打不出来，异常信息一进 Windows 终端就炸。
    return f"CNY {amount_micro / MICRO:.4f}"


class ApiKeyStore:
    """API Key、用量账本与限额。明文 key 只在创建时返回一次。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._rates_cache: tuple[float, list[dict[str, Any]]] | None = None
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
            for column in ("daily_credit_quota", *LIMIT_COLUMNS.values()):
                if column not in columns:
                    db.execute(f"ALTER TABLE api_keys ADD COLUMN {column} INTEGER")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_charges (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    key_id TEXT,
                    job_id TEXT,
                    service TEXT NOT NULL,
                    model TEXT,
                    channel TEXT,
                    unit TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    amount_micro INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    job_status TEXT,
                    source TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS usage_charges_time_idx ON usage_charges(created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_charges_key_time_idx ON usage_charges(key_id, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_charges_model_idx ON usage_charges(model, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_charges_job_idx ON usage_charges(job_id)")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_rates (
                    service TEXT NOT NULL,
                    model TEXT NOT NULL,
                    variant TEXT NOT NULL DEFAULT '*',
                    unit TEXT NOT NULL,
                    amount_micro INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    note TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service, model, variant)
                )
                """
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS billing_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self._migrate(db)

    def _migrate(self, db: sqlite3.Connection) -> None:
        """把 11-05 那版「每日积分额度 + 积分账本」搬进金额口径。幂等。"""
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # 每日积分额度 → 24 小时金额额度（62 积分 = ¥1）
        if "daily_credit_quota" in {row["name"] for row in db.execute("PRAGMA table_info(api_keys)")}:
            db.execute(
                """UPDATE api_keys SET limit_24h_micro=CAST(ROUND(daily_credit_quota * 1000000.0 / 62) AS INTEGER)
                   WHERE daily_credit_quota IS NOT NULL AND limit_24h_micro IS NULL"""
            )
        if "api_key_credit_charges" in tables:
            db.execute(
                """INSERT OR IGNORE INTO usage_charges
                   (id,request_id,key_id,job_id,service,model,channel,unit,quantity,amount_micro,state,source,created_at,settled_at)
                   SELECT job_id, job_id, key_id, job_id, 'video_generation', NULL, NULL, 'credit', credits,
                          CAST(ROUND(credits * 1000000.0 / 62) AS INTEGER),
                          CASE state WHEN 'spent' THEN 'spent' WHEN 'refunded' THEN 'refunded' ELSE 'held' END,
                          'legacy', created_at,
                          CASE state WHEN 'held' THEN NULL ELSE updated_at END
                   FROM api_key_credit_charges"""
            )
        # 费率表为空时才灌种子，避免把用户改过的覆盖掉
        if db.execute("SELECT COUNT(*) FROM usage_rates").fetchone()[0] == 0:
            stamp = _now()
            db.executemany(
                """INSERT INTO usage_rates(service,model,variant,unit,amount_micro,source,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [(*row, stamp) for row in seed_rates()],
            )
        for key, value in (
            ("platform_limit_5h_micro", ""),
            ("platform_limit_24h_micro", ""),
            ("platform_limit_total_micro", ""),
            ("credits_per_yuan", "62"),
        ):
            db.execute(
                "INSERT OR IGNORE INTO billing_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, value, _now()),
            )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    # ---- 额度 ----

    @staticmethod
    def _sum(db: sqlite3.Connection, since: str, key_id: str | None) -> int:
        clause = "created_at>=? AND state!='refunded'"
        params: list[Any] = [since]
        if key_id is not None:
            clause += " AND key_id=?"
            params.append(key_id)
        row = db.execute(f"SELECT COALESCE(SUM(amount_micro),0) FROM usage_charges WHERE {clause}", params).fetchone()
        return int(row[0] or 0)

    @classmethod
    def _spent(cls, db: sqlite3.Connection, key_id: str | None) -> dict[str, int]:
        return {window: cls._sum(db, _since(window), key_id) for window in WINDOWS}

    @classmethod
    def _limits(cls, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, int | None]:
        keys = row.keys()
        return {window: (row[column] if column in keys else None) for window, column in LIMIT_COLUMNS.items()}

    @staticmethod
    def _platform_limits(db: sqlite3.Connection) -> dict[str, int | None]:
        rows = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM billing_settings")}
        result: dict[str, int | None] = {}
        for window in WINDOWS:
            raw = rows.get(f"platform_limit_{window}_micro") or ""
            result[window] = int(raw) if raw.strip() else None
        return result

    def _public(self, row: sqlite3.Row, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        limits = self._limits(db, row) if db is not None else {window: None for window in WINDOWS}
        result = {
            "id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
            "quota": row["quota"], "used": row["used"],
            "remaining": None if row["quota"] is None else max(0, row["quota"] - row["used"]),
            "expires_at": row["expires_at"], "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            "limits": limits,
        }
        if db is not None:
            spent = self._spent(db, row["id"])
            result["spent"] = spent
            result["spent_yuan"] = {window: to_yuan(value) for window, value in spent.items()}
            result["limits_yuan"] = {window: to_yuan(value) for window, value in limits.items()}
        return result

    # ---- Key 生命周期 ----

    def create(self, name: str, quota: int | None, expires_at: str | None,
               limit_5h_micro: int | None = None, limit_24h_micro: int | None = None,
               limit_total_micro: int | None = None) -> dict[str, Any]:
        plaintext = "aic_" + secrets.token_urlsafe(32)
        prefix = plaintext[:12]
        identifier = str(uuid4())
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO api_keys(id,name,key_prefix,key_hash,quota,limit_5h_micro,limit_24h_micro,limit_total_micro,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identifier, name.strip(), prefix, self._hash(plaintext), quota,
                 limit_5h_micro, limit_24h_micro, limit_total_micro, expires_at, _now()),
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
        allowed = {"name", "quota", "expires_at", "enabled", *LIMIT_COLUMNS.values()}
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

    # ---- 账本 ----

    def hold(self, key_id: str | None, request_id: str, service: str, model: str | None,
             channel: str | None, quote: Quote) -> dict[str, Any]:
        """预占一单的额度。超限抛 CreditQuotaExceeded，此时一行都不写。

        预扣而不是事后结算：预扣才能在上游真正花钱之前挡住；作业失败或取消时由
        settle() 退掉，所以窗口里的金额只包含还没结束的和成功的。
        """
        amount = max(quote.amount_micro, 0)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key_id is not None:
                row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
                if row is None:
                    return {"charged": 0, "spent": {}, "limits": {}}
                limits = self._limits(db, row)
                spent = self._spent(db, key_id)
                for window in ("5h", "24h", "total"):
                    limit = limits[window]
                    if limit is not None and spent[window] + amount > limit:
                        raise CreditQuotaExceeded(
                            f"key {window} limit exceeded: used {_yuan(spent[window])} + this job {_yuan(amount)} > limit {_yuan(limit)}",
                            reason=f"key_{window}",
                        )
            else:
                limits, spent = {}, {}

            platform_limits = self._platform_limits(db)
            platform_spent = self._spent(db, None)
            for window in ("5h", "24h", "total"):
                limit = platform_limits[window]
                if limit is not None and platform_spent[window] + amount > limit:
                    raise CreditQuotaExceeded(
                        f"platform {window} limit exceeded: used {_yuan(platform_spent[window])} + this job {_yuan(amount)} > limit {_yuan(limit)}",
                        reason=f"platform_{window}",
                    )

            stamp = _now()
            charge_id = str(uuid4())
            db.execute(
                """INSERT INTO usage_charges
                   (id,request_id,key_id,job_id,service,model,channel,unit,quantity,amount_micro,state,source,created_at)
                   VALUES(?,?,?,NULL,?,?,?,?,?,?,'held',?,?)""",
                (charge_id, request_id, key_id, service, model, channel,
                 quote.unit, quote.quantity, amount, quote.source, stamp),
            )
            return {
                "charge_id": charge_id,
                "charged": amount,
                "spent": spent,
                "limits": limits,
                "platform_spent": platform_spent,
                "platform_limits": platform_limits,
            }

    def link(self, charge_id: str, job_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE usage_charges SET job_id=? WHERE id=?", (job_id, charge_id))

    def settle(self, request_id: str, status: str, job_status: str | None = None) -> None:
        """作业终结时结算：成功落定，失败/取消退额度。重复调用无副作用。"""
        if status not in {"succeeded", "failed", "cancelled"}:
            return
        state = "spent" if status == "succeeded" else "refunded"
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE usage_charges SET state=?,job_status=?,settled_at=? WHERE request_id=? AND state='held'",
                (state, job_status or status, _now(), request_id),
            )

    def settle_job(self, job_id: str, status: str, job_status: str | None = None) -> None:
        """轮询口手里只有 job_id，按 job_id 结算（作业创建时 link 过）。"""
        if status not in {"succeeded", "failed", "cancelled"}:
            return
        state = "spent" if status == "succeeded" else "refunded"
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE usage_charges SET state=?,job_status=?,settled_at=? WHERE job_id=? AND state='held'",
                (state, job_status or status, _now(), job_id),
            )

    def touch(self, request_id: str, job_status: str) -> None:
        """作业还在跑时把最新状态写进明细，页面才看得出「进行中」到哪一步。"""
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE usage_charges SET job_status=? WHERE request_id=? AND state='held'",
                (job_status, request_id),
            )

    def usage(self, key_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            if row is None:
                return {"spent": {}, "limits": {}}
            spent = self._spent(db, key_id)
            return {
                "spent": spent,
                "limits": self._limits(db, row),
                "spent_yuan": {window: to_yuan(value) for window, value in spent.items()},
            }

    # ---- 费率与平台设置 ----

    def rates(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM usage_rates ORDER BY service,model,variant").fetchall()
            return [dict(row) | {"yuan": round(row["amount_micro"] / MICRO, 6)} for row in rows]

    def rates_cached(self, ttl: float = 5.0) -> list[dict[str, Any]]:
        """每个请求都要查一次费率表，缓存几秒；改了费率最多 5 秒生效。"""
        now = datetime.now(UTC).timestamp()
        cached = self._rates_cache
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        value = self.rates()
        self._rates_cache = (now, value)
        return value

    def invalidate_rates(self) -> None:
        self._rates_cache = None

    def set_rate(self, service: str, model: str, variant: str, unit: str,
                 amount_micro: int, source: str = "manual", note: str | None = None,
                 enabled: bool = True) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO usage_rates(service,model,variant,unit,amount_micro,source,note,enabled,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(service,model,variant) DO UPDATE SET
                     unit=excluded.unit, amount_micro=excluded.amount_micro, source=excluded.source,
                     note=excluded.note, enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (service, model, variant, unit, amount_micro, source, note, int(enabled), _now()),
            )
            row = db.execute(
                "SELECT * FROM usage_rates WHERE service=? AND model=? AND variant=?",
                (service, model, variant),
            ).fetchone()
            return dict(row)

    def delete_rate(self, service: str, model: str, variant: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "DELETE FROM usage_rates WHERE service=? AND model=? AND variant=?",
                (service, model, variant),
            )
        return cursor.rowcount > 0

    def settings(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM billing_settings")}
        return {
            "platform_limit_5h_micro": int(rows["platform_limit_5h_micro"]) if rows.get("platform_limit_5h_micro") else None,
            "platform_limit_24h_micro": int(rows["platform_limit_24h_micro"]) if rows.get("platform_limit_24h_micro") else None,
            "platform_limit_total_micro": int(rows["platform_limit_total_micro"]) if rows.get("platform_limit_total_micro") else None,
            "credits_per_yuan": float(rows.get("credits_per_yuan") or 62),
        }

    def set_settings(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            for key, value in values.items():
                if key not in SETTING_KEYS:
                    continue
                db.execute(
                    """INSERT INTO billing_settings(key,value,updated_at) VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    (key, "" if value is None else str(value), _now()),
                )
        return self.settings()

    # ---- 查询 ----

    _FILTERS = ("service", "model", "channel", "state", "key_id")

    @classmethod
    def _where(cls, *, start: str | None = None, end: str | None = None, service: str | None = None,
               model: str | None = None, channel: str | None = None, state: str | None = None,
               key_id: str | None = None, keyword: str | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("c.created_at>=?")
            params.append(start)
        if end:
            clauses.append("c.created_at<?")
            params.append(end)
        for column, value in (("c.service", service), ("c.model", model), ("c.channel", channel),
                              ("c.state", state), ("c.key_id", key_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if keyword:
            clauses.append("(c.model LIKE ? OR c.channel LIKE ? OR c.service LIKE ? OR c.note LIKE ? OR k.name LIKE ?)")
            params.extend([f"%{keyword}%"] * 5)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    def charges(self, *, page: int = 1, page_size: int = 50, **filters: Any) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), 500)
        where, params = self._where(**filters)
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM usage_charges c LEFT JOIN api_keys k ON k.id=c.key_id {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""SELECT c.*, k.name key_name FROM usage_charges c LEFT JOIN api_keys k ON k.id=c.key_id
                    {where} ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        items = [
            dict(row) | {
                "key_name": row["key_name"] or ("服务令牌" if row["key_id"] is None else row["key_id"][:8]),
                "yuan": round(row["amount_micro"] / MICRO, 6),
            }
            for row in rows
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def summary(self, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        where, params = self._where(start=start, end=end, service=None, model=None, channel=None,
                                    state=None, key_id=None, keyword=None)
        # 退回的账（作业失败/取消）不算消耗，与窗口合计 _sum 同一口径；明细页仍然看得到。
        where = "WHERE c.state!='refunded'" + (f" AND {where[6:]}" if where else "")
        base = f"FROM usage_charges c LEFT JOIN api_keys k ON k.id=c.key_id {where}"
        with self._connect() as db:
            total_amount, count = db.execute(
                f"SELECT COALESCE(SUM(c.amount_micro),0), COUNT(*) {base}", params
            ).fetchone()
            by_model = self._group(db, base, params, "COALESCE(c.model,'(未知)')", "model")
            by_channel = self._group(db, base, params, "COALESCE(c.channel,'(未指定)')", "channel")
            by_service = self._group(db, base, params, "c.service", "service")
            by_key = self._group(db, base, params, "COALESCE(k.name,'服务令牌')", "key")
            daily = [
                dict(row)
                for row in db.execute(
                    f"""SELECT substr(c.created_at,1,10) day, COALESCE(SUM(c.amount_micro),0) amount_micro, COUNT(*) count
                        {base} GROUP BY day ORDER BY day""",
                    params,
                )
            ]
            now_spent = self._spent(db, None)
            platform_limits = self._platform_limits(db)
        for row in daily:
            row["yuan"] = to_yuan(row["amount_micro"])
        return {
            "amount_micro": int(total_amount or 0),
            "amount_yuan": to_yuan(int(total_amount or 0)),
            "count": int(count or 0),
            "by_model": by_model,
            "by_channel": by_channel,
            "by_service": by_service,
            "by_key": by_key,
            "daily": daily,
            "platform": {
                "spent": now_spent,
                "spent_yuan": {window: to_yuan(value) for window, value in now_spent.items()},
                "limits": platform_limits,
                "limits_yuan": {window: to_yuan(value) for window, value in platform_limits.items()},
            },
        }

    @staticmethod
    def _group(db: sqlite3.Connection, base: str, params: list[Any], expression: str, alias: str) -> list[dict[str, Any]]:
        rows = db.execute(
            f"""SELECT {expression} AS {alias}, COALESCE(SUM(c.amount_micro),0) amount_micro, COUNT(*) count
                {base} GROUP BY {alias} ORDER BY amount_micro DESC""",
            params,
        ).fetchall()
        return [dict(row) | {"yuan": to_yuan(row["amount_micro"])} for row in rows]
