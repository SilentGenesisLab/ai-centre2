from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .credit_pricing import MICRO, Quote, seed_rates, to_yuan

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")

WINDOWS = {"5h": timedelta(hours=5), "24h": timedelta(hours=24), "total": None}
LIMIT_COLUMNS = {"5h": "limit_5h_micro", "24h": "limit_24h_micro", "total": "limit_total_micro"}
# 模型白/黑名单，存 JSON 数组串；NULL 或空数组都表示「没有这一档限制」。
MODEL_COLUMNS = ("allowed_models", "blocked_models")
SETTING_KEYS = {
    "platform_limit_5h_micro",
    "platform_limit_24h_micro",
    "platform_limit_total_micro",
    "credits_per_yuan",
    # 活动促销：到期的型号按 0 元记账，到期后自动失效（不动 usage_rates 里的原价行）。
    "promo_free_items",
    "promo_free_until",
}


class CreditQuotaExceeded(Exception):
    """这个 Key（或整个平台）剩下的额度不够这一单了。

    `reason` 指出是哪一条线拦住的，调用方据此报 429 并让人一眼看出该调哪个数。
    """

    def __init__(self, message: str, reason: str = "limit"):
        super().__init__(message)
        self.reason = reason


class ModelNotAllowed(Exception):
    """这个 Key 的模型白/黑名单不允许这一单（与钱无关，报 403）。

    `reason`：`blocked` = 命中黑名单；`not_allowed` = 不在白名单里。
    """

    def __init__(self, message: str, reason: str = "blocked"):
        super().__init__(message)
        self.reason = reason


class ConcurrencyLimitExceeded(Exception):
    """这个 Key 在途的作业已经到上限了（与钱无关，报 429——等一会儿就能再提）。

    存在的理由：`max_concurrent_jobs` 是按 Key 的在途作业数（queued/running/cancel_requested），
    不是金额额度。没有它，一把 Key 投几百条长视频就能把整个上游账号池占满，
    别人的请求既付钱也排不进去。
    """

    def __init__(self, message: str, reason: str = "key_concurrency"):
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


def _promo_items(raw: str | None) -> list[dict[str, Any]]:
    """促销型号清单存成 JSON 串（`[{"service":...,"model":...}]`），坏数据当没有。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("service") and item.get("model")]


def _model_list(raw: Any) -> list[str]:
    """白/黑名单存成 JSON 数组串（也可能已经是 list），坏数据当空。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _matches(patterns: list[str], model: str) -> str | None:
    """命中就返回命中的那条规则，没命中返回 None。支持 `*`/`?` 通配，大小写不敏感。"""
    target = model.strip().lower()
    for pattern in patterns:
        try:
            if fnmatchcase(target, pattern.lower()):
                return pattern
        except Exception:
            continue
    return None


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
            for column in MODEL_COLUMNS:
                if column not in columns:
                    db.execute(f"ALTER TABLE api_keys ADD COLUMN {column} TEXT")
            # 按 Key 的在途作业数上限。DEFAULT 8 让存量 Key 一并拿到 8（用户选的默认值），
            # 之后显式改成 NULL 就是「不限」，不会被迁移再改回去——列已存在就不再 ADD。
            if "max_concurrent_jobs" not in columns:
                db.execute("ALTER TABLE api_keys ADD COLUMN max_concurrent_jobs INTEGER DEFAULT 8")
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

    @staticmethod
    def _dump_models(value: Any) -> str | None:
        """list → JSON 串；None/空表 → NULL（两种空法都表示「没有这一档限制」）。"""
        if value is None:
            return None
        items = _model_list(value)
        return json.dumps(items, ensure_ascii=False) if items else None

    @staticmethod
    def _check_models(row: sqlite3.Row, model: str | None) -> None:
        """白/黑名单共同约束：黑名单优先，白名单为空即全部可用。

        型号缺失（有些端点把型号放在别的字段、要到运行时才定）时跳过——判不了就不拦。
        """
        if not model or not str(model).strip():
            return
        blocked = _model_list(row["blocked_models"])
        hit = _matches(blocked, str(model))
        if hit:
            raise ModelNotAllowed(
                f"model '{model}' is blocked for this api key (rule '{hit}')", reason="blocked"
            )
        allowed = _model_list(row["allowed_models"])
        if allowed and not _matches(allowed, str(model)):
            raise ModelNotAllowed(
                f"model '{model}' is not allowed for this api key; allowed: {', '.join(allowed[:10])}"
                + (" …" if len(allowed) > 10 else ""),
                reason="not_allowed",
            )

    # ---- 额度 ----

    def concurrency_limit(self, key_id: str | None) -> int | None:
        """这个 Key 允许的在途作业数；None = 不限（服务令牌内部调用也不限）。"""
        if key_id is None:
            return None
        with self._connect() as db:
            row = db.execute("SELECT max_concurrent_jobs FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

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
            "max_concurrent_jobs": row["max_concurrent_jobs"] if "max_concurrent_jobs" in row.keys() else None,
            "allowed_models": _model_list(row["allowed_models"]),
            "blocked_models": _model_list(row["blocked_models"]),
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
               limit_total_micro: int | None = None,
               allowed_models: list[str] | None = None, blocked_models: list[str] | None = None,
               max_concurrent_jobs: int | None = 8) -> dict[str, Any]:
        plaintext = "aic_" + secrets.token_urlsafe(32)
        prefix = plaintext[:12]
        identifier = str(uuid4())
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO api_keys(id,name,key_prefix,key_hash,quota,limit_5h_micro,limit_24h_micro,limit_total_micro,
                                        allowed_models,blocked_models,max_concurrent_jobs,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, name.strip(), prefix, self._hash(plaintext), quota,
                 limit_5h_micro, limit_24h_micro, limit_total_micro,
                 self._dump_models(allowed_models), self._dump_models(blocked_models),
                 max_concurrent_jobs, expires_at, _now()),
            )
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (identifier,)).fetchone()
            result = self._public(row, db)
        result["api_key"] = plaintext
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
            return [self._public(row, db) for row in rows]

    def key(self, key_id: str) -> dict[str, Any] | None:
        """单个 Key 的公开字段（含限额与已花金额）。给 /v1/balance 用。"""
        with self._connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            return self._public(row, db) if row else None

    def update(self, identifier: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"name", "quota", "expires_at", "enabled", "max_concurrent_jobs",
                   *LIMIT_COLUMNS.values(), *MODEL_COLUMNS}
        values = {key: value for key, value in changes.items() if key in allowed}
        if "name" in values:
            values["name"] = str(values["name"]).strip()
        for column in MODEL_COLUMNS:
            if column in values:
                values[column] = self._dump_models(values[column])
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
        """预占一单的额度。超限抛 CreditQuotaExceeded、型号被 Key 限制抛 ModelNotAllowed，此时一行都不写。

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
                # 型号限制先判：这个模型压根不给用，就不用去看还剩多少钱了。
                self._check_models(row, model)
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

    def set_charge_model(self, charge_id: str, model: str) -> None:
        """提交时还不知道型号的端点（TTS 按 provider 回退），事后补记。"""
        with self._lock, self._connect() as db:
            db.execute("UPDATE usage_charges SET model=? WHERE id=?", (model, charge_id))

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

    def held_charges(self, older_than_hours: float) -> list[dict[str, Any]]:
        """预占超过这个时长还没落定的账，给对账清扫用。"""
        cutoff = (datetime.now(UTC) - timedelta(hours=older_than_hours)).isoformat(timespec="milliseconds")
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,request_id,job_id,service,model,amount_micro,created_at FROM usage_charges "
                "WHERE state='held' AND created_at < ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def rates_for_billing(self) -> list[dict[str, Any]]:
        """估价用的费率：原价行 + 活动促销行。

        促销是**整型号覆盖**：被促销的型号，它的原价行（含按分辨率细分的行）必须整条
        剔除，否则 `seedance-2.0-fast/720p` 这种精确变体行在打分上会压过 `variant='*'`
        的促销行（2*3+2 > 2*3+0），结果是促销看着生效、实际照原价扣。
        """
        rates = self.rates_cached()
        promo = self._active_promo()
        if not promo:
            return rates
        covered = {(row["service"], row["model"]) for row in promo}
        return [row for row in rates if (row["service"], row["model"]) not in covered] + promo

    def _active_promo(self) -> list[dict[str, Any]]:
        settings = self.settings()
        items = settings.get("promo_free_items") or []
        until = settings.get("promo_free_until")
        if not items or not until:
            return []
        try:
            deadline = datetime.fromisoformat(str(until))
        except ValueError:
            return []
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=SHANGHAI)
        if datetime.now(UTC) >= deadline.astimezone(UTC):
            return []
        stamp = _now()
        return [
            {
                "service": str(item.get("service") or ""),
                "model": str(item.get("model") or ""),
                "variant": "*",
                "unit": str(item.get("unit") or "second"),
                "amount_micro": 0,
                "source": "promo",
                "note": f"活动促销至 {until}",
                "enabled": 1,
                "updated_at": stamp,
            }
            for item in items
            if isinstance(item, Mapping) and item.get("service") and item.get("model")
        ]

    def promo_active(self) -> bool:
        return bool(self._active_promo())

    def promo_rows(self) -> list[dict[str, Any]]:
        """当前生效的活动促销行（没有活动就是空表）。价目表要展示它就调这个。"""
        return self._active_promo()

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
            "promo_free_items": _promo_items(rows.get("promo_free_items")),
            "promo_free_until": rows.get("promo_free_until") or None,
        }

    def set_settings(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            for key, value in values.items():
                if key not in SETTING_KEYS:
                    continue
                if key == "promo_free_items":
                    value = json.dumps(list(value or []), ensure_ascii=False)
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
            # 有些服务没有「型号」这个概念（超分、切分、去水印……），model 存的是空串。
            # 那种行按服务名归类，比在用量页上堆一排「(未知)」有用。
            by_model = self._group(db, base, params,
                                   "COALESCE(NULLIF(c.model,''), c.service)", "model")
            by_channel = self._group(db, base, params, "COALESCE(c.channel,'(未指定)')", "channel")
            by_service = self._group(db, base, params, "c.service", "service")
            # 删掉的 Key 留下的是历史账，不能混进「服务令牌」（那是内部调用，key_id 为 NULL）。
            by_key = self._group(
                db, base, params,
                "COALESCE(k.name, CASE WHEN c.key_id IS NULL THEN '服务令牌' ELSE '(已删除的 Key)' END)",
                "key",
            )

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

    def usage_report(self, *, start: str | None = None, end: str | None = None,
                     key_id: str | None = None) -> dict[str, Any]:
        """按模型汇总：每个模型被调了多少次、花了多少。key_id 为 None 时是全平台。

        与 summary 同一口径：退回的账不算消耗，明细里仍然看得到。
        """
        clauses = ["c.state!='refunded'"]
        params: list[Any] = []
        if start:
            clauses.append("c.created_at>=?")
            params.append(start)
        if end:
            clauses.append("c.created_at<?")
            params.append(end)
        if key_id is not None:
            clauses.append("c.key_id=?")
            params.append(key_id)
        where = "WHERE " + " AND ".join(clauses)
        with self._connect() as db:
            amount, count = db.execute(
                f"SELECT COALESCE(SUM(c.amount_micro),0), COUNT(*) FROM usage_charges c {where}",
                params,
            ).fetchone()
            rows = db.execute(
                f"""SELECT c.service service, COALESCE(NULLIF(c.model,''), c.service) model,
                           COALESCE(SUM(c.amount_micro),0) amount_micro, COUNT(*) count
                    FROM usage_charges c {where}
                    GROUP BY c.service, c.model ORDER BY amount_micro DESC, count DESC""",
                params,
            ).fetchall()
        return {
            "amount_micro": int(amount or 0),
            "amount_yuan": to_yuan(int(amount or 0)),
            "count": int(count or 0),
            "by_model": [dict(row) | {"amount_yuan": to_yuan(row["amount_micro"])} for row in rows],
        }

    def usage_windows(self, *, key_id: str | None = None) -> dict[str, dict[str, Any]]:
        """5h / 24h / 累计三个滚动窗口的金额与调用次数，看「现在花掉多少」用这个。"""
        result: dict[str, dict[str, Any]] = {}
        with self._connect() as db:
            for window in WINDOWS:
                since = _since(window)
                clause = "state!='refunded'" + (" AND created_at>=?" if since else "")
                params: list[Any] = [since] if since else []
                if key_id is not None:
                    clause += " AND key_id=?"
                    params.append(key_id)
                amount, count = db.execute(
                    f"SELECT COALESCE(SUM(amount_micro),0), COUNT(*) FROM usage_charges WHERE {clause}",
                    params,
                ).fetchone()
                result[window] = {
                    "amount_micro": int(amount or 0),
                    "amount_yuan": to_yuan(int(amount or 0)),
                    "count": int(count or 0),
                }
        return result

    @staticmethod
    def _group(db: sqlite3.Connection, base: str, params: list[Any], expression: str, alias: str) -> list[dict[str, Any]]:
        rows = db.execute(
            f"""SELECT {expression} AS {alias}, COALESCE(SUM(c.amount_micro),0) amount_micro, COUNT(*) count
                {base} GROUP BY {alias} ORDER BY amount_micro DESC""",
            params,
        ).fetchall()
        return [dict(row) | {"yuan": to_yuan(row["amount_micro"])} for row in rows]
