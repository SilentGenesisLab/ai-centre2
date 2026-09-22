from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
MAX_SNAPSHOT_BYTES = 256 * 1024
TASK_DETAIL_CALL_LIMIT = 10
TERMINAL_SUCCESS = {"completed", "succeeded", "success", "ok"}
TERMINAL_FAILURE = {"failed", "failure", "error", "cancelled", "canceled", "timeout"}
RUNNING = {"queued", "pending", "received", "running", "started", "retrying", "progress"}
SECRET_KEY_RE = re.compile(
    r"authorization|cookie|token|secret|password|api[_-]?key|access[_-]?key",
    re.IGNORECASE,
)
URL_KEY_RE = re.compile(r"(?:^|_)(?:url|uri)$|source_uri|file_url", re.IGNORECASE)


@dataclass(frozen=True)
class RouteInfo:
    service: str
    operation: str
    creates_task: bool = False
    async_task: bool = False
    status_query: bool = False
    cancel: bool = False


@dataclass
class CapturedCall:
    call_id: str
    trace_id: str
    started_at: str
    method: str
    path: str
    query: str
    client_address: str
    token_fingerprint: str | None
    request_content_type: str
    request_body: bytes
    request_bytes: int
    status_code: int
    response_content_type: str
    response_headers: dict[str, str]
    response_body: bytes
    response_bytes: int
    finished_at: str
    duration_ms: float
    disconnected: bool = False


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds")


def sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "[invalid-url]"
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return value
    query = urlencode([(key, "***") for key, _ in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def sanitize_query(value: str) -> str:
    if not value:
        return ""
    return urlencode([(key, "***") for key, _ in parse_qsl(value, keep_blank_values=True)])


def redact_value(value: Any, key: str = "", *, summary: bool = False,
                 expose_business_text: bool = False) -> Any:
    if SECRET_KEY_RE.search(key):
        return "***"
    if isinstance(value, dict):
        return {
            str(item_key): redact_value(item_value, str(item_key), summary=summary,
                                        expose_business_text=expose_business_text)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        if summary and len(value) > 20:
            return [
                *[redact_value(item, key, summary=True, expose_business_text=expose_business_text)
                  for item in value[:20]],
                {"_truncated_items": len(value) - 20},
            ]
        return [redact_value(item, key, summary=summary,
                             expose_business_text=expose_business_text) for item in value]
    if isinstance(value, str):
        if URL_KEY_RE.search(key) or value.startswith(("http://", "https://")):
            return sanitize_url(value)
        if summary and not expose_business_text and key.lower() in {
            "text",
            "prompt_text",
            "prompt",
            "prompts",
            "transcript",
            "transcription",
            "content",
            "emotion",
            "instruction",
            "recognized_text",
        }:
            return {"present": bool(value), "characters": len(value)}
        if summary and expose_business_text and len(value) > 8_000:
            return f"{value[:8_000]}…[{len(value)} chars]"
        if summary and len(value) > 160:
            return f"{value[:160]}…[{len(value)} chars]"
    return value


def decode_json_body(content_type: str, body: bytes) -> Any | None:
    if "json" not in content_type.lower() or not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def compact_json(value: Any, limit: int = MAX_SNAPSHOT_BYTES) -> tuple[bytes, bool]:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) <= limit:
        return raw, False
    marker = {
        "_truncated": True,
        "original_bytes": len(raw),
        "preview": raw[: max(0, limit - 256)].decode("utf-8", errors="ignore"),
    }
    return json.dumps(marker, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), True


def route_info(method: str, path: str) -> RouteInfo | None:
    method = method.upper()
    exact_creates: dict[tuple[str, str], RouteInfo] = {
        ("POST", "/v1/asr/transcriptions"): RouteInfo("asr", "transcribe", True),
        ("POST", "/v1/asr/transcriptions/upload"): RouteInfo("asr", "transcribe_upload", True),
        ("POST", "/v1/tts/speech"): RouteInfo("tts", "synthesize_legacy", True),
        ("POST", "/v2/tts/speech"): RouteInfo("tts", "synthesize", True),
        ("POST", "/v2/tts/speech/upload"): RouteInfo("tts", "synthesize_upload", True),
        ("POST", "/v2/tts/speech/stream"): RouteInfo("tts", "synthesize_stream", True),
        ("POST", "/v2/tts/jobs"): RouteInfo("tts", "synthesize_async", True, True),
        ("POST", "/v1/lipsync/jobs"): RouteInfo("lipsync", "create", True, True),
        ("POST", "/v1/lipsync/jobs/upload"): RouteInfo("lipsync", "create_upload", True, True),
        ("POST", "/v1/ocr/batch"): RouteInfo("ocr", "batch", True),
        ("POST", "/internal/admin/ocr/batch"): RouteInfo("ocr", "batch_upload", True),
        ("POST", "/v1/face-mosaic/jobs"): RouteInfo("face", "process", True, True),
        ("POST", "/v1/face-mosaic/jobs/wait"): RouteInfo("face", "process_wait", True, True),
        ("POST", "/v1/video-scenes/jobs"): RouteInfo("scene", "detect", True, True),
        ("POST", "/v1/video-scenes/jobs/wait"): RouteInfo("scene", "detect_wait", True, True),
        ("POST", "/v1/video-reviews/jobs"): RouteInfo("video_review", "review", True, True),
        ("POST", "/v1/watermark-removal/jobs"): RouteInfo("watermark", "process", True, True),
        ("POST", "/v1/watermark-removal/jobs/wait"): RouteInfo("watermark", "process_wait", True, True),
        ("POST", "/v1/video-depth/jobs"): RouteInfo("depth", "infer", True, True),
        ("POST", "/v1/video-depth/jobs/wait"): RouteInfo("depth", "infer_wait", True, True),
        ("POST", "/v1/audio-separation/jobs"): RouteInfo("separation", "separate", True, True),
        ("POST", "/v1/audio-separation/jobs/wait"): RouteInfo("separation", "separate_wait", True, True),
        ("POST", "/v1/video-upscale/jobs"): RouteInfo("upscale", "upscale", True, True),
        ("POST", "/v1/video-upscale/jobs/wait"): RouteInfo("upscale", "upscale_wait", True, True),
        ("POST", "/v1/video-generations/minimax-h3/jobs"): RouteInfo("h3", "generate", True, True),
        ("POST", "/v1/video-generations/jobs"): RouteInfo("video_generation", "generate", True, True),
        ("POST", "/v1/image-generations/jobs"): RouteInfo("image_generation", "generate", True, True),
        ("POST", "/v1/uploads"): RouteInfo("storage", "upload_public"),
        ("POST", "/internal/admin/storage/upload"): RouteInfo("storage", "upload"),
        ("POST", "/internal/admin/subtitle/detect"): RouteInfo("subtitle", "detect", True),
    }
    if info := exact_creates.get((method, path)):
        return info
    patterns: list[tuple[re.Pattern[str], RouteInfo]] = [
        (re.compile(r"^/v1/lipsync/jobs/[^/]+$"), RouteInfo("lipsync", "status", status_query=True)),
        (re.compile(r"^/v2/tts/jobs/[^/]+$"), RouteInfo("tts", "job_status", status_query=True)),
        (re.compile(r"^/v1/face-mosaic/jobs/[^/]+$"), RouteInfo("face", "status", status_query=True)),
        (re.compile(r"^/v1/video-scenes/jobs/[^/]+$"), RouteInfo("scene", "status", status_query=True)),
        (re.compile(r"^/v1/video-reviews/jobs/[^/]+$"), RouteInfo("video_review", "status", status_query=True)),
        (re.compile(r"^/v1/video-reviews/jobs/[^/]+/report$"), RouteInfo("video_review", "report", status_query=True)),
        (re.compile(r"^/v1/watermark-removal/jobs/[^/]+$"), RouteInfo("watermark", "status", status_query=True)),
        (re.compile(r"^/v1/video-depth/jobs/[^/]+$"), RouteInfo("depth", "status", status_query=True)),
        (re.compile(r"^/v1/audio-separation/jobs/[^/]+$"), RouteInfo("separation", "status", status_query=True)),
        (re.compile(r"^/v1/video-upscale/jobs/[^/]+$"), RouteInfo("upscale", "status", status_query=True)),
        (re.compile(r"^/v1/video-generations/minimax-h3/jobs/[^/]+$"), RouteInfo("h3", "status", status_query=True)),
        (re.compile(r"^/v1/video-generations/jobs/[^/]+$"), RouteInfo("video_generation", "status", status_query=True)),
        (re.compile(r"^/v1/image-generations/jobs/[^/]+$"), RouteInfo("image_generation", "status", status_query=True)),
        (re.compile(r"^/v1/lipsync/jobs/[^/]+/cancel$"), RouteInfo("lipsync", "cancel", cancel=True)),
        (re.compile(r"^/v1/face-mosaic/jobs/[^/]+/cancel$"), RouteInfo("face", "cancel", cancel=True)),
        (re.compile(r"^/v1/video-scenes/jobs/[^/]+/cancel$"), RouteInfo("scene", "cancel", cancel=True)),
        (re.compile(r"^/v1/video-reviews/jobs/[^/]+/cancel$"), RouteInfo("video_review", "cancel", cancel=True)),
        (re.compile(r"^/v1/watermark-removal/jobs/[^/]+/cancel$"), RouteInfo("watermark", "cancel", cancel=True)),
        (re.compile(r"^/v1/video-depth/jobs/[^/]+/cancel$"), RouteInfo("depth", "cancel", cancel=True)),
        (re.compile(r"^/v1/audio-separation/jobs/[^/]+/cancel$"), RouteInfo("separation", "cancel", cancel=True)),
        (re.compile(r"^/v1/video-upscale/jobs/[^/]+/cancel$"), RouteInfo("upscale", "cancel", cancel=True)),
        (re.compile(r"^/v1/video-generations/minimax-h3/jobs/[^/]+/cancel$"), RouteInfo("h3", "cancel", cancel=True)),
        (re.compile(r"^/v1/video-generations/jobs/[^/]+/cancel$"), RouteInfo("video_generation", "cancel", cancel=True)),
        (re.compile(r"^/v1/image-generations/jobs/[^/]+/cancel$"), RouteInfo("image_generation", "cancel", cancel=True)),
    ]
    for pattern, info in patterns:
        if pattern.match(path):
            return info
    if path.startswith("/v1/lipsync/"):
        return RouteInfo("lipsync", "read")
    if path.startswith("/v1/asr/"):
        return RouteInfo("asr", "read")
    if path.startswith(("/v1/tts/", "/v2/tts/")):
        return RouteInfo("tts", "read")
    if path.startswith("/v1/ocr/"):
        return RouteInfo("ocr", "read")
    if path.startswith("/v1/face-mosaic/"):
        return RouteInfo("face", "read")
    if path.startswith("/v1/video-scenes/"):
        return RouteInfo("scene", "read")
    if path.startswith("/v1/video-reviews/"):
        return RouteInfo("video_review", "read")
    if path.startswith("/v1/watermark-removal/"):
        return RouteInfo("watermark", "read")
    if path.startswith("/v1/video-depth/"):
        return RouteInfo("depth", "read")
    if path.startswith("/v1/audio-separation/"):
        return RouteInfo("separation", "read")
    if path.startswith("/v1/video-upscale/"):
        return RouteInfo("upscale", "read")
    if path.startswith("/v1/video-generations/minimax-h3/"):
        return RouteInfo("h3", "read")
    if path.startswith("/v1/video-generations/"):
        return RouteInfo("video_generation", "read")
    if path.startswith("/v1/image-generations/"):
        return RouteInfo("image_generation", "read")
    return None


def external_job_id(path: str, response: Any | None) -> str | None:
    patterns = (
        r"^/v1/lipsync/jobs/([^/]+)",
        r"^/v2/tts/jobs/([^/]+)",
        r"^/v1/face-mosaic/jobs/([^/]+)",
        r"^/v1/video-scenes/jobs/([^/]+)",
        r"^/v1/video-reviews/jobs/([^/]+)",
        r"^/v1/watermark-removal/jobs/([^/]+)",
        r"^/v1/video-depth/jobs/([^/]+)",
        r"^/v1/audio-separation/jobs/([^/]+)",
        r"^/v1/video-upscale/jobs/([^/]+)",
        r"^/v1/video-generations/minimax-h3/jobs/([^/]+)",
        r"^/v1/video-generations/jobs/([^/]+)",
        r"^/v1/image-generations/jobs/([^/]+)",
    )
    for pattern in patterns:
        match = re.match(pattern, path)
        if match and match.group(1) not in {"wait", "upload"}:
            return match.group(1)
    if isinstance(response, dict):
        for key in ("job_id", "task_id", "request_id"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def response_status(response: Any | None, status_code: int) -> str:
    if isinstance(response, dict):
        for key in ("status", "state"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value.lower()
    return "succeeded" if 200 <= status_code < 300 else "failed"


def response_stage(response: Any | None, fallback: str) -> str:
    if isinstance(response, dict):
        value = response.get("stage")
        if isinstance(value, str) and value:
            return value
    return fallback


def error_code(response: Any | None, status_code: int) -> str | None:
    if status_code < 400:
        return None
    if isinstance(response, dict):
        for key in ("code", "error_code", "detail"):
            value = response.get(key)
            if isinstance(value, str):
                return value[:160]
    return f"http_{status_code}"


def usage_from_payload(service: str, request: Any | None, response: Any | None, headers: dict[str, str]) -> tuple[Decimal, str]:
    if service == "tts" and isinstance(request, dict):
        text = request.get("text")
        if isinstance(text, str):
            return Decimal(len(text)) / Decimal(1000), "1000_chars"
    if service == "ocr" and isinstance(request, dict):
        images = request.get("images")
        if isinstance(images, list):
            return Decimal(len(images)), "image"
    if service == "asr" and isinstance(response, dict):
        ends = [
            item.get("end")
            for item in response.get("segments", [])
            if isinstance(item, dict) and isinstance(item.get("end"), int | float)
        ]
        if ends:
            return Decimal(str(max(ends))) / Decimal(60), "audio_minute"
    if service == "separation" and isinstance(response, dict):
        value = response.get("duration_seconds")
        if isinstance(value, int | float) and value >= 0:
            return Decimal(str(value)) / Decimal(60), "audio_minute"
    if service == "h3" and isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, dict):
            value = output.get("duration_seconds")
            if isinstance(value, int | float) and value >= 0:
                return Decimal(str(value)) / Decimal(60), "video_minute"
    if service in {"lipsync", "face", "scene", "watermark", "depth", "upscale", "h3", "video_review"} and isinstance(response, dict):
        source = response.get("source")
        if service == "video_review" and isinstance(source, dict):
            value = source.get("duration_sec")
            if isinstance(value, int | float) and value >= 0:
                return Decimal(str(value)) / Decimal(60), "video_minute"
        for key in ("video_duration_seconds", "source_duration_seconds", "duration_seconds"):
            value = response.get(key)
            if isinstance(value, int | float) and value >= 0:
                return Decimal(str(value)) / Decimal(60), "video_minute"
    duration_ms = headers.get("x-audio-duration-ms")
    if service == "tts" and duration_ms:
        try:
            return Decimal(duration_ms) / Decimal(1000), "output_second"
        except Exception:
            pass
    return Decimal(0), "task"


class PayloadCipher:
    def __init__(self, secret: str) -> None:
        key = hashlib.sha256(secret.encode("utf-8")).digest()
        self._cipher = AESGCM(key)

    def encrypt(self, payload: bytes) -> tuple[str, str]:
        nonce = __import__("os").urandom(12)
        encrypted = self._cipher.encrypt(nonce, payload, None)
        return base64.b64encode(nonce).decode("ascii"), base64.b64encode(encrypted).decode("ascii")

    def decrypt(self, nonce: str, ciphertext: str) -> bytes:
        return self._cipher.decrypt(base64.b64decode(nonce), base64.b64decode(ciphertext), None)


class ObservabilityStore:
    def __init__(
        self,
        path: Path,
        encryption_secret: str,
        fingerprint_secret: str,
        payload_retention_days: int = 30,
        record_retention_days: int = 365,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = PayloadCipher(encryption_secret)
        self._fingerprint_secret = fingerprint_secret.encode("utf-8")
        self.payload_retention_days = payload_retention_days
        self.record_retention_days = record_retention_days
        self._lock = threading.RLock()
        self._dropped_events = 0
        self._last_error: str | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS api_calls (
          id TEXT PRIMARY KEY, trace_id TEXT NOT NULL UNIQUE, started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
          query TEXT NOT NULL DEFAULT '', service TEXT NOT NULL, operation TEXT NOT NULL,
          status_code INTEGER NOT NULL, success INTEGER NOT NULL, duration_ms REAL NOT NULL,
          request_bytes INTEGER NOT NULL DEFAULT 0, response_bytes INTEGER NOT NULL DEFAULT 0,
          client_address TEXT NOT NULL, token_fingerprint TEXT, task_id TEXT,
          error_code TEXT, request_summary TEXT, response_summary TEXT,
          disconnected INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, external_task_id TEXT, service TEXT NOT NULL, operation TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'live',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          duration_ms REAL, input_quantity TEXT NOT NULL DEFAULT '0', unit_type TEXT NOT NULL DEFAULT 'task',
          quality_json TEXT, result_summary TEXT, error_code TEXT, first_call_id TEXT,
          UNIQUE(service, external_task_id)
        );
        CREATE TABLE IF NOT EXISTS task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, timestamp TEXT NOT NULL,
          level TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL,
          details_json TEXT, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS payload_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, kind TEXT NOT NULL,
          nonce TEXT NOT NULL, ciphertext TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          UNIQUE(call_id, kind), FOREIGN KEY(call_id) REFERENCES api_calls(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS pricing_rules (
          id TEXT PRIMARY KEY, service TEXT NOT NULL, operation TEXT NOT NULL,
          unit_type TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'CNY',
          fixed_fee TEXT NOT NULL DEFAULT '0', unit_price TEXT NOT NULL DEFAULT '0',
          effective_from TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'admin'
        );
        CREATE TABLE IF NOT EXISTS billing_ledger (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, api_call_id TEXT,
          pricing_rule_id TEXT, service TEXT NOT NULL, operation TEXT NOT NULL,
          quantity TEXT NOT NULL, unit_type TEXT NOT NULL, currency TEXT NOT NULL,
          fixed_fee TEXT NOT NULL, unit_price TEXT NOT NULL, amount TEXT NOT NULL,
          waste_amount TEXT NOT NULL, status TEXT NOT NULL, rule_snapshot TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_metrics (
          day TEXT NOT NULL, service TEXT NOT NULL, operation TEXT NOT NULL,
          calls INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0,
          failures INTEGER NOT NULL DEFAULT 0, duration_total_ms REAL NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL, PRIMARY KEY(day, service, operation)
        );
        CREATE INDEX IF NOT EXISTS idx_calls_started ON api_calls(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_calls_service ON api_calls(service, operation, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_calls_task ON api_calls(task_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(service, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_analytics ON tasks(service, operation, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_rules_lookup ON pricing_rules(service, operation, effective_from DESC);
        """
        with self._lock, self._connection() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('collection_started_at', ?)",
                (iso_utc(),),
            )
            connection.execute("PRAGMA optimize")
        os.chmod(self.path, 0o600)

    def safe_record(self, call: CapturedCall) -> None:
        try:
            self.record(call)
            self._last_error = None
        except Exception as exc:
            self._dropped_events += 1
            self._last_error = type(exc).__name__

    def token_fingerprint(self, authorization: str) -> str | None:
        if not authorization:
            return None
        value = authorization.removeprefix("Bearer ").strip()
        if not value:
            return None
        return hmac.new(self._fingerprint_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()[:20]

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def record(self, call: CapturedCall) -> None:
        info = route_info(call.method, call.path)
        if info is None:
            return
        request_payload = decode_json_body(call.request_content_type, call.request_body)
        response_payload = decode_json_body(call.response_content_type, call.response_body)
        request_summary = redact_value(
            request_payload, summary=True, expose_business_text=True
        ) if request_payload is not None else {
            "content_type": call.request_content_type or None,
            "bytes": call.request_bytes,
        }
        response_summary = redact_value(response_payload, summary=True) if response_payload is not None else {
            "content_type": call.response_content_type or None,
            "bytes": call.response_bytes,
        }
        task_id = self._update_task(call, info, request_payload, response_payload, response_summary)
        error = error_code(response_payload, call.status_code)
        day = datetime.fromisoformat(call.started_at).astimezone(SHANGHAI).date().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO api_calls(
                  id, trace_id, started_at, finished_at, method, path, query, service, operation,
                  status_code, success, duration_ms, request_bytes, response_bytes, client_address,
                  token_fingerprint, task_id, error_code, request_summary, response_summary, disconnected
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    call.call_id, call.trace_id, call.started_at, call.finished_at, call.method,
                    call.path, sanitize_query(call.query), info.service, info.operation, call.status_code,
                    int(200 <= call.status_code < 300), call.duration_ms, call.request_bytes,
                    call.response_bytes, call.client_address, call.token_fingerprint, task_id,
                    error, json.dumps(request_summary, ensure_ascii=False),
                    json.dumps(response_summary, ensure_ascii=False), int(call.disconnected),
                ),
            )
            connection.execute(
                """INSERT INTO daily_metrics(day, service, operation, calls, successes, failures, duration_total_ms, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(day, service, operation) DO UPDATE SET
                     calls=calls+1, successes=successes+excluded.successes,
                     failures=failures+excluded.failures,
                     duration_total_ms=duration_total_ms+excluded.duration_total_ms,
                     updated_at=excluded.updated_at""",
                (
                    day, info.service, info.operation, 1,
                    int(200 <= call.status_code < 300), int(call.status_code >= 400),
                    call.duration_ms, call.finished_at,
                ),
            )
            self._store_snapshot(connection, call.call_id, "request", request_payload)
            self._store_snapshot(connection, call.call_id, "response", response_payload)
            if task_id:
                connection.execute(
                    "UPDATE tasks SET first_call_id=COALESCE(first_call_id, ?) WHERE id=?",
                    (call.call_id, task_id),
                )
                self._finalize_billing(connection, task_id, call.call_id)
            connection.execute("COMMIT")

    def _store_snapshot(self, connection: sqlite3.Connection, call_id: str, kind: str, payload: Any | None) -> None:
        if payload is None:
            return
        protected = redact_value(payload, summary=False)
        raw, truncated = compact_json(protected)
        nonce, ciphertext = self._cipher.encrypt(raw)
        created = utc_now()
        connection.execute(
            """INSERT OR REPLACE INTO payload_snapshots(
                 call_id, kind, nonce, ciphertext, truncated, created_at, expires_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                call_id, kind, nonce, ciphertext, int(truncated), iso_utc(created),
                iso_utc(created + timedelta(days=self.payload_retention_days)),
            ),
        )

    def _find_task(self, service: str, external_id: str | None) -> dict[str, Any] | None:
        if not external_id:
            return None
        with self._lock, self._connection() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM tasks WHERE service=? AND external_task_id=?",
                    (service, external_id),
                ).fetchone()
            )

    def _update_task(
        self,
        call: CapturedCall,
        info: RouteInfo,
        request: Any | None,
        response: Any | None,
        response_summary: Any,
    ) -> str | None:
        external_id = external_job_id(call.path, response)
        existing = self._find_task(info.service, external_id)
        if not (info.creates_task or info.status_query or info.cancel or existing):
            return None
        task_id = existing["id"] if existing else str(uuid4())
        old_status = existing["status"] if existing else None
        task_operation = (
            existing["operation"]
            if existing and (info.status_query or info.cancel)
            else info.operation
        )
        status_value = response_status(response, call.status_code)
        if info.cancel and 200 <= call.status_code < 300:
            status_value = "cancel_requested"
        elif info.async_task and status_value in TERMINAL_SUCCESS and not (
            isinstance(response, dict) and status_value in {str(response.get("status", "")).lower(), str(response.get("state", "")).lower()}
        ):
            status_value = "queued"
        if info.creates_task and info.async_task and status_value == "succeeded" and isinstance(response, dict):
            explicit = str(response.get("status") or response.get("state") or "").lower()
            status_value = explicit or "queued"
        stage = response_stage(response, "completed" if status_value in TERMINAL_SUCCESS else status_value)
        quantity, unit_type = usage_from_payload(info.service, request, response, call.response_headers)
        finished_at = call.finished_at if status_value in TERMINAL_SUCCESS | TERMINAL_FAILURE else None
        started_at = call.started_at if status_value not in {"queued", "pending"} else None
        duration = None
        if finished_at:
            if existing and info.status_query:
                duration = max(0.0, (
                    datetime.fromisoformat(call.finished_at)
                    - datetime.fromisoformat(str(existing["created_at"]))
                ).total_seconds() * 1000)
            else:
                duration = call.duration_ms
        result_summary = json.dumps(response_summary, ensure_ascii=False) if response_summary is not None else None
        error = error_code(response, call.status_code)
        now = call.finished_at
        with self._lock, self._connection() as connection:
            if existing:
                connection.execute(
                    """UPDATE tasks SET operation=?, status=?, stage=?, updated_at=?,
                       started_at=COALESCE(started_at, ?), finished_at=COALESCE(finished_at, ?),
                       duration_ms=COALESCE(duration_ms, ?), input_quantity=CASE WHEN ?!='0' THEN ? ELSE input_quantity END,
                       unit_type=CASE WHEN ?!='task' THEN ? ELSE unit_type END,
                       result_summary=COALESCE(?, result_summary), error_code=COALESCE(?, error_code)
                       WHERE id=?""",
                    (
                        task_operation, status_value, stage, now, started_at, finished_at, duration,
                        str(quantity), str(quantity), unit_type, unit_type, result_summary, error, task_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO tasks(
                       id, external_task_id, service, operation, status, stage, created_at, updated_at,
                       started_at, finished_at, duration_ms, input_quantity, unit_type, result_summary, error_code
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, external_id, info.service, task_operation, status_value, stage,
                        call.started_at, now, started_at, finished_at, duration, str(quantity),
                        unit_type, result_summary, error,
                    ),
                )
            if old_status != status_value:
                connection.execute(
                    "INSERT INTO task_events(task_id, timestamp, level, stage, message, details_json) VALUES(?,?,?,?,?,?)",
                    (
                        task_id, now, "error" if status_value in TERMINAL_FAILURE else "info", stage,
                        f"任务状态变更为 {status_value}",
                        json.dumps({"previous_status": old_status, "status": status_value}, ensure_ascii=False),
                    ),
                )
        return task_id

    def _finalize_billing(self, connection: sqlite3.Connection, task_id: str, call_id: str) -> None:
        if connection.execute("SELECT 1 FROM billing_ledger WHERE task_id=?", (task_id,)).fetchone():
            return
        task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["status"] not in TERMINAL_SUCCESS | TERMINAL_FAILURE:
            return
        rule = connection.execute(
            """SELECT * FROM pricing_rules WHERE enabled=1 AND service=?
               AND operation IN (?, '*') AND effective_from<=?
               ORDER BY CASE WHEN operation=? THEN 0 ELSE 1 END, effective_from DESC LIMIT 1""",
            (task["service"], task["operation"], task["created_at"], task["operation"]),
        ).fetchone()
        quantity = Decimal(task["input_quantity"] or "0")
        if rule:
            fixed_fee = Decimal(rule["fixed_fee"])
            unit_price = Decimal(rule["unit_price"])
            currency = rule["currency"]
            rule_id = rule["id"]
            rule_snapshot = json.dumps(dict(rule), ensure_ascii=False)
            ledger_status = "billed" if task["status"] in TERMINAL_SUCCESS else "waste"
        else:
            fixed_fee = unit_price = Decimal(0)
            currency = "CNY"
            rule_id = None
            rule_snapshot = None
            ledger_status = "unpriced"
        total = (fixed_fee + quantity * unit_price).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        amount = total if task["status"] in TERMINAL_SUCCESS else Decimal(0)
        waste = total if task["status"] in TERMINAL_FAILURE else Decimal(0)
        connection.execute(
            """INSERT INTO billing_ledger(
               id, task_id, api_call_id, pricing_rule_id, service, operation, quantity, unit_type,
               currency, fixed_fee, unit_price, amount, waste_amount, status, rule_snapshot, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid4()), task_id, call_id, rule_id, task["service"], task["operation"],
                str(quantity), task["unit_type"], currency, str(fixed_fee), str(unit_price),
                str(amount), str(waste), ledger_status, rule_snapshot, iso_utc(),
            ),
        )

    @staticmethod
    def _window(from_date: str | None, to_date: str | None, days: int = 7) -> tuple[str, str]:
        today = datetime.now(SHANGHAI).date()
        start_day = date.fromisoformat(from_date) if from_date else today - timedelta(days=days - 1)
        end_day = date.fromisoformat(to_date) if to_date else today
        if end_day < start_day or (end_day - start_day).days > 366:
            raise ValueError("date range must be between 1 and 367 days")
        start = datetime.combine(start_day, time.min, SHANGHAI).astimezone(UTC)
        end = datetime.combine(end_day + timedelta(days=1), time.min, SHANGHAI).astimezone(UTC)
        return iso_utc(start), iso_utc(end)

    def overview(self, from_date: str | None = None, to_date: str | None = None, service: str | None = None) -> dict[str, Any]:
        start, end = self._window(from_date, to_date)
        where = "started_at>=? AND started_at<?"
        params: list[Any] = [start, end]
        task_where = "created_at>=? AND created_at<?"
        task_params: list[Any] = [start, end]
        if service:
            where += " AND service=?"
            task_where += " AND service=?"
            params.append(service)
            task_params.append(service)
        with self._lock, self._connection() as connection:
            calls = connection.execute(
                f"""SELECT COUNT(*) calls, SUM(success) successes, AVG(duration_ms) average_ms
                    FROM api_calls WHERE {where}""",
                params,
            ).fetchone()
            durations = [row[0] for row in connection.execute(
                f"SELECT duration_ms FROM api_calls WHERE {where} ORDER BY duration_ms", params
            ).fetchall()]
            task_counts = self._rows(connection.execute(
                f"SELECT status, COUNT(*) count FROM tasks WHERE {task_where} GROUP BY status", task_params
            ).fetchall())
            services = self._rows(connection.execute(
                f"""SELECT service, COUNT(*) calls, SUM(success) successes, AVG(duration_ms) average_ms
                    FROM api_calls WHERE {where} GROUP BY service ORDER BY calls DESC""",
                params,
            ).fetchall())
            trends = self._rows(connection.execute(
                f"""SELECT substr(datetime(started_at, '+8 hours'),1,10) day, service,
                    COUNT(*) calls, SUM(success) successes, AVG(duration_ms) average_ms
                    FROM api_calls WHERE {where} GROUP BY day, service ORDER BY day""",
                params,
            ).fetchall())
            task_trends = self._rows(connection.execute(
                f"""SELECT substr(datetime(created_at, '+8 hours'),1,10) day, service,
                    COUNT(*) tasks,
                    SUM(CASE WHEN status IN ('completed','succeeded','success','ok') THEN 1 ELSE 0 END) successes,
                    SUM(CASE WHEN status IN ('failed','failure','error','cancelled','canceled','timeout') THEN 1 ELSE 0 END) failures
                    FROM tasks WHERE {task_where} GROUP BY day, service ORDER BY day""",
                task_params,
            ).fetchall())
            errors = self._rows(connection.execute(
                f"""SELECT COALESCE(error_code, 'unknown') error_code, COUNT(*) count
                    FROM api_calls WHERE {where} AND success=0
                    GROUP BY error_code ORDER BY count DESC LIMIT 10""",
                params,
            ).fetchall())
            billing_where = task_where.replace("created_at", "t.created_at").replace("service=?", "t.service=?")
            billing = connection.execute(
                f"""SELECT COALESCE(SUM(CAST(b.amount AS REAL)),0) amount,
                    COALESCE(SUM(CAST(b.waste_amount AS REAL)),0) waste,
                    SUM(CASE WHEN b.status='unpriced' THEN 1 ELSE 0 END) unpriced
                    FROM billing_ledger b JOIN tasks t ON t.id=b.task_id WHERE {billing_where}""",
                task_params,
            ).fetchone()
            billing_trends = self._rows(connection.execute(
                f"""SELECT substr(datetime(t.created_at, '+8 hours'),1,10) day, t.service,
                    COALESCE(SUM(CAST(b.amount AS REAL)),0) amount,
                    COALESCE(SUM(CAST(b.waste_amount AS REAL)),0) waste_amount
                    FROM billing_ledger b JOIN tasks t ON t.id=b.task_id
                    WHERE {billing_where} GROUP BY day, t.service ORDER BY day""",
                task_params,
            ).fetchall())
            collection = connection.execute(
                "SELECT value FROM metadata WHERE key='collection_started_at'"
            ).fetchone()
        total = int(calls["calls"] or 0)
        successes = int(calls["successes"] or 0)
        task_map = {item["status"]: item["count"] for item in task_counts}
        terminal_tasks = sum(task_map.get(item, 0) for item in TERMINAL_SUCCESS | TERMINAL_FAILURE)
        successful_tasks = sum(task_map.get(item, 0) for item in TERMINAL_SUCCESS)
        return {
            "range": {"from": start, "to": end, "timezone": "Asia/Shanghai"},
            "collection_started_at": collection[0] if collection else None,
            "calls": total,
            "call_successes": successes,
            "call_success_rate": successes / total if total else None,
            "task_success_rate": successful_tasks / terminal_tasks if terminal_tasks else None,
            "task_statuses": task_map,
            "average_ms": float(calls["average_ms"] or 0),
            "p50_ms": self._percentile(durations, 0.50),
            "p95_ms": self._percentile(durations, 0.95),
            "p99_ms": self._percentile(durations, 0.99),
            "amount": float(billing["amount"] or 0),
            "waste_amount": float(billing["waste"] or 0),
            "unpriced_tasks": int(billing["unpriced"] or 0),
            "services": services,
            "trend": trends,
            "task_trend": task_trends,
            "billing_trend": billing_trends,
            "errors": errors,
        }

    def analytics_summary(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        service: str | None = None,
        operation: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        start, end = self._window(from_date, to_date)
        call_clauses = ["started_at>=?", "started_at<?"]
        call_params: list[Any] = [start, end]
        task_clauses = ["created_at>=?", "created_at<?"]
        task_params: list[Any] = [start, end]
        if service:
            call_clauses.append("service=?")
            task_clauses.append("service=?")
            call_params.append(service)
            task_params.append(service)
        if operation:
            call_clauses.append("operation=?")
            task_clauses.append("operation=?")
            call_params.append(operation)
            task_params.append(operation)
        if status:
            call_clauses.append("task_id IN (SELECT id FROM tasks WHERE status=?)")
            call_params.append(status)
            task_clauses.append("status=?")
            task_params.append(status)
        call_where = " AND ".join(call_clauses)
        task_where = " AND ".join(task_clauses)
        with self._lock, self._connection() as connection:
            calls = self._rows(connection.execute(
                f"SELECT service,operation,success,duration_ms,error_code,started_at FROM api_calls WHERE {call_where} ORDER BY started_at",
                call_params,
            ).fetchall())
            tasks = self._rows(connection.execute(
                f"""SELECT id,service,operation,status,duration_ms,input_quantity,unit_type,
                           created_at,finished_at,error_code FROM tasks WHERE {task_where}""",
                task_params,
            ).fetchall())
            billing_where = task_where.replace("created_at", "t.created_at").replace(
                "service=?", "t.service=?"
            ).replace("operation=?", "t.operation=?").replace("status=?", "t.status=?")
            billing = connection.execute(
                f"""SELECT COALESCE(SUM(CAST(b.amount AS REAL)),0),
                            COALESCE(SUM(CAST(b.waste_amount AS REAL)),0),
                            SUM(CASE WHEN b.status='unpriced' THEN 1 ELSE 0 END)
                     FROM billing_ledger b JOIN tasks t ON t.id=b.task_id WHERE {billing_where}""",
                task_params,
            ).fetchone()
            collection = connection.execute(
                "SELECT value FROM metadata WHERE key='collection_started_at'"
            ).fetchone()

        terminal = [task for task in tasks if task["status"] in TERMINAL_SUCCESS | TERMINAL_FAILURE]
        successful = [task for task in terminal if task["status"] in TERMINAL_SUCCESS]
        task_durations = [float(task["duration_ms"]) for task in successful if task["duration_ms"] is not None]
        call_durations = [float(call["duration_ms"]) for call in calls]
        endpoint_keys = sorted({(str(call["service"]), str(call["operation"])) for call in calls} |
                               {(str(task["service"]), str(task["operation"])) for task in tasks})
        endpoints = []
        for endpoint_service, endpoint_operation in endpoint_keys:
            endpoint_calls = [call for call in calls if call["service"] == endpoint_service and call["operation"] == endpoint_operation]
            endpoint_tasks = [task for task in tasks if task["service"] == endpoint_service and task["operation"] == endpoint_operation]
            endpoint_terminal = [task for task in endpoint_tasks if task["status"] in TERMINAL_SUCCESS | TERMINAL_FAILURE]
            endpoint_success = [task for task in endpoint_terminal if task["status"] in TERMINAL_SUCCESS]
            endpoint_task_durations = [float(task["duration_ms"]) for task in endpoint_success if task["duration_ms"] is not None]
            endpoints.append({
                "service": endpoint_service,
                "operation": endpoint_operation,
                "calls": len(endpoint_calls),
                "call_successes": sum(bool(call["success"]) for call in endpoint_calls),
                "call_success_rate": (sum(bool(call["success"]) for call in endpoint_calls) / len(endpoint_calls)) if endpoint_calls else None,
                "tasks": len(endpoint_tasks),
                "terminal_tasks": len(endpoint_terminal),
                "task_successes": len(endpoint_success),
                "task_success_rate": len(endpoint_success) / len(endpoint_terminal) if endpoint_terminal else None,
                "api_latency_ms": self._metric_summary(float(call["duration_ms"]) for call in endpoint_calls),
                "task_latency_ms": self._metric_summary(endpoint_task_durations),
                "last_error": next((str(call["error_code"]) for call in reversed(endpoint_calls) if call.get("error_code")), None),
            })
        endpoints.sort(key=lambda item: item["calls"], reverse=True)
        usage: dict[str, float] = {}
        for task in tasks:
            try:
                usage[str(task["unit_type"])] = usage.get(str(task["unit_type"]), 0.0) + float(task["input_quantity"] or 0)
            except (TypeError, ValueError):
                continue
        errors: dict[str, int] = {}
        for call in calls:
            if not call["success"]:
                code = str(call.get("error_code") or "unknown")
                errors[code] = errors.get(code, 0) + 1
        return {
            "range": {"from": start, "to": end, "timezone": "Asia/Shanghai"},
            "collection_started_at": collection[0] if collection else None,
            "filters": {"service": service, "operation": operation, "status": status},
            "api": {
                "calls": len(calls),
                "successes": sum(bool(call["success"]) for call in calls),
                "success_rate": sum(bool(call["success"]) for call in calls) / len(calls) if calls else None,
                "latency_ms": self._metric_summary(call_durations),
            },
            "tasks": {
                "total": len(tasks), "terminal": len(terminal), "succeeded": len(successful),
                "failed": sum(task["status"] in TERMINAL_FAILURE for task in terminal),
                "success_rate": len(successful) / len(terminal) if terminal else None,
                "latency_ms": self._metric_summary(task_durations),
                "statuses": {value: sum(task["status"] == value for task in tasks) for value in sorted({str(task["status"]) for task in tasks})},
            },
            "usage": [{"unit_type": key, "quantity": round(value, 6)} for key, value in sorted(usage.items())],
            "billing": {
                "amount": float(billing[0] or 0), "waste_amount": float(billing[1] or 0),
                "unpriced_tasks": int(billing[2] or 0),
            },
            "endpoints": endpoints,
            "errors": [{"error_code": key, "count": value} for key, value in sorted(errors.items(), key=lambda item: item[1], reverse=True)[:10]],
        }

    def analytics_timeseries(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        service: str | None = None,
        operation: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        start, end = self._window(from_date, to_date)
        call_clauses = ["started_at>=?", "started_at<?"]
        call_params: list[Any] = [start, end]
        task_clauses = ["created_at>=?", "created_at<?"]
        task_params: list[Any] = [start, end]
        for field, value in (("service", service), ("operation", operation)):
            if value:
                call_clauses.append(f"{field}=?")
                task_clauses.append(f"{field}=?")
                call_params.append(value)
                task_params.append(value)
        if status:
            call_clauses.append("task_id IN (SELECT id FROM tasks WHERE status=?)")
            call_params.append(status)
            task_clauses.append("status=?")
            task_params.append(status)
        with self._lock, self._connection() as connection:
            calls = self._rows(connection.execute(
                f"SELECT started_at,success,duration_ms FROM api_calls WHERE {' AND '.join(call_clauses)}",
                call_params,
            ).fetchall())
            tasks = self._rows(connection.execute(
                f"SELECT created_at,status,duration_ms FROM tasks WHERE {' AND '.join(task_clauses)}",
                task_params,
            ).fetchall())
        days: dict[str, dict[str, Any]] = {}
        for call in calls:
            day = datetime.fromisoformat(str(call["started_at"])).astimezone(SHANGHAI).date().isoformat()
            bucket = days.setdefault(day, {"day": day, "calls": 0, "call_successes": 0, "call_latencies": [], "tasks": 0, "task_successes": 0, "task_failures": 0, "task_latencies": []})
            bucket["calls"] += 1
            bucket["call_successes"] += int(bool(call["success"]))
            bucket["call_latencies"].append(float(call["duration_ms"]))
        for task in tasks:
            day = datetime.fromisoformat(str(task["created_at"])).astimezone(SHANGHAI).date().isoformat()
            bucket = days.setdefault(day, {"day": day, "calls": 0, "call_successes": 0, "call_latencies": [], "tasks": 0, "task_successes": 0, "task_failures": 0, "task_latencies": []})
            bucket["tasks"] += 1
            if task["status"] in TERMINAL_SUCCESS:
                bucket["task_successes"] += 1
                if task["duration_ms"] is not None:
                    bucket["task_latencies"].append(float(task["duration_ms"]))
            elif task["status"] in TERMINAL_FAILURE:
                bucket["task_failures"] += 1
        points = []
        for day in sorted(days):
            bucket = days[day]
            points.append({
                "day": day, "calls": bucket["calls"], "call_successes": bucket["call_successes"],
                "tasks": bucket["tasks"], "task_successes": bucket["task_successes"], "task_failures": bucket["task_failures"],
                "api_latency_ms": self._metric_summary(bucket["call_latencies"]),
                "task_latency_ms": self._metric_summary(bucket["task_latencies"]),
            })
        return {"range": {"from": start, "to": end, "timezone": "Asia/Shanghai"}, "points": points}

    def analytics_stages(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        service: str | None = None,
        operation: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        start, end = self._window(from_date, to_date)
        clauses = ["created_at>=?", "created_at<?", "status IN ('completed','succeeded','success','ok')", "duration_ms IS NOT NULL"]
        params: list[Any] = [start, end]
        for field, value in (("service", service), ("operation", operation), ("status", status)):
            if value:
                clauses.append(f"{field}=?")
                params.append(value)
        with self._lock, self._connection() as connection:
            rows = self._rows(connection.execute(
                f"SELECT service,duration_ms FROM tasks WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall())
        by_service: dict[str, list[float]] = {}
        for row in rows:
            by_service.setdefault(str(row["service"]), []).append(float(row["duration_ms"]))
        return {
            "range": {"from": start, "to": end, "timezone": "Asia/Shanghai"},
            "stages": [{
                "service": item_service,
                "stage": "end_to_end",
                "label": "端到端",
                "latency_ms": self._metric_summary(values),
                "source": "measured",
            } for item_service, values in sorted(by_service.items())],
            "note": "未接入精确阶段计时的服务仅展示端到端任务耗时",
        }

    @classmethod
    def _metric_summary(cls, values: Iterable[float]) -> dict[str, float | int | None]:
        data = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
        return {
            "n": len(data),
            "mean": round(sum(data) / len(data), 3) if data else None,
            "p50": round(cls._percentile(data, 0.50), 3) if data else None,
            "p95": round(cls._percentile(data, 0.95), 3) if data else None,
            "p99": round(cls._percentile(data, 0.99), 3) if data else None,
            "min": round(data[0], 3) if data else None,
            "max": round(data[-1], 3) if data else None,
        }

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        index = min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))
        return float(values[index])

    def list_calls(self, *, page: int = 1, page_size: int = 50, service: str | None = None,
                   status: str | None = None, trace_id: str | None = None,
                   task_id: str | None = None, from_date: str | None = None,
                   to_date: str | None = None) -> dict[str, Any]:
        start, end = self._window(from_date, to_date, 30)
        clauses = ["started_at>=?", "started_at<?"]
        params: list[Any] = [start, end]
        if service:
            clauses.append("service=?")
            params.append(service)
        if status == "success":
            clauses.append("success=1")
        elif status == "failure":
            clauses.append("success=0")
        if trace_id:
            clauses.append("trace_id LIKE ?")
            params.append(f"%{trace_id}%")
        if task_id:
            clauses.append("task_id LIKE ?")
            params.append(f"%{task_id}%")
        where = " AND ".join(clauses)
        offset = (page - 1) * page_size
        with self._lock, self._connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM api_calls WHERE {where}", params).fetchone()[0]
            rows = self._rows(connection.execute(
                f"SELECT * FROM api_calls WHERE {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall())
        for row in rows:
            row["success"] = bool(row["success"])
            row["disconnected"] = bool(row["disconnected"])
            row["request_summary"] = json.loads(row["request_summary"] or "null")
            row["response_summary"] = json.loads(row["response_summary"] or "null")
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def call_detail(self, call_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = self._row(connection.execute("SELECT * FROM api_calls WHERE id=?", (call_id,)).fetchone())
            if not row:
                return None
            snapshots = self._rows(connection.execute(
                "SELECT kind, truncated, expires_at FROM payload_snapshots WHERE call_id=?", (call_id,)
            ).fetchall())
        row["success"] = bool(row["success"])
        row["request_summary"] = json.loads(row["request_summary"] or "null")
        row["response_summary"] = json.loads(row["response_summary"] or "null")
        row["snapshots"] = snapshots
        return row

    def reveal(self, call_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT kind, nonce, ciphertext, truncated, expires_at FROM payload_snapshots WHERE call_id=?",
                (call_id,),
            ).fetchall()
        if not rows:
            return None
        result: dict[str, Any] = {}
        for row in rows:
            raw = self._cipher.decrypt(row["nonce"], row["ciphertext"])
            result[row["kind"]] = {
                "payload": json.loads(raw),
                "truncated": bool(row["truncated"]),
                "expires_at": row["expires_at"],
            }
        return result

    def list_tasks(self, *, page: int = 1, page_size: int = 50, service: str | None = None,
                   status: str | None = None, task_id: str | None = None,
                   from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        start, end = self._window(from_date, to_date, 30)
        clauses = ["t.created_at>=?", "t.created_at<?"]
        params: list[Any] = [start, end]
        if service:
            clauses.append("t.service=?")
            params.append(service)
        if status:
            clauses.append("t.status=?")
            params.append(status)
        if task_id:
            clauses.append("(t.id LIKE ? OR t.external_task_id LIKE ?)")
            params.extend([f"%{task_id}%", f"%{task_id}%"])
        where = " AND ".join(clauses)
        offset = (page - 1) * page_size
        with self._lock, self._connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM tasks t WHERE {where}", params).fetchone()[0]
            rows = self._rows(connection.execute(
                f"""SELECT t.*, b.amount, b.waste_amount, b.currency, b.status billing_status
                    FROM tasks t LEFT JOIN billing_ledger b ON b.task_id=t.id
                    WHERE {where} ORDER BY t.updated_at DESC LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ).fetchall())
        for row in rows:
            row["result_summary"] = json.loads(row["result_summary"] or "null")
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def task_detail(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = self._row(connection.execute(
                "SELECT * FROM tasks WHERE id=? OR external_task_id=? LIMIT 1", (task_id, task_id)
            ).fetchone())
            if not row:
                return None
            row["events"] = self._rows(connection.execute(
                "SELECT timestamp, level, stage, message, details_json FROM task_events WHERE task_id=? ORDER BY id",
                (row["id"],),
            ).fetchall())
            row["calls_total"] = connection.execute(
                "SELECT COUNT(*) FROM api_calls WHERE task_id=?", (row["id"],)
            ).fetchone()[0]
            row["calls"] = self._rows(connection.execute(
                """SELECT id, trace_id, started_at, method, path, status_code, duration_ms,
                          request_summary, response_summary
                   FROM api_calls WHERE task_id=? ORDER BY started_at DESC LIMIT ?""",
                (row["id"], TASK_DETAIL_CALL_LIMIT),
            ).fetchall())
            row["billing"] = self._row(connection.execute(
                "SELECT * FROM billing_ledger WHERE task_id=?", (row["id"],)
            ).fetchone())
        row["result_summary"] = json.loads(row["result_summary"] or "null")
        row["quality_json"] = json.loads(row["quality_json"] or "null")
        for call in row["calls"]:
            call["request_summary"] = json.loads(call["request_summary"] or "null")
            call["response_summary"] = json.loads(call["response_summary"] or "null")
        for event in row["events"]:
            event["details"] = json.loads(event.pop("details_json") or "null")
        if row["billing"] and row["billing"].get("rule_snapshot"):
            row["billing"]["rule_snapshot"] = json.loads(row["billing"]["rule_snapshot"])
        return row

    def active_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in RUNNING | {"cancel_requested"})
        statuses = sorted(RUNNING | {"cancel_requested"})
        with self._lock, self._connection() as connection:
            return self._rows(connection.execute(
                f"""SELECT id, external_task_id, service, operation, status
                    FROM tasks WHERE external_task_id IS NOT NULL AND status IN ({placeholders})
                    ORDER BY updated_at LIMIT ?""",
                [*statuses, limit],
            ).fetchall())

    def reconcile_task(self, service: str, external_task_id_value: str, payload: dict[str, Any]) -> None:
        status_value = response_status(payload, 200)
        stage = response_stage(payload, status_value)
        now = iso_utc()
        summary = redact_value(payload, summary=True)
        quantity, unit_type = usage_from_payload(service, None, payload, {})
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM tasks WHERE service=? AND external_task_id=?",
                (service, external_task_id_value),
            ).fetchone()
            if not task:
                connection.execute("ROLLBACK")
                return
            finished_at = now if status_value in TERMINAL_SUCCESS | TERMINAL_FAILURE else None
            duration_ms = None
            if finished_at:
                duration_ms = max(
                    0.0,
                    (datetime.fromisoformat(now) - datetime.fromisoformat(task["created_at"])).total_seconds() * 1000,
                )
            connection.execute(
                """UPDATE tasks SET status=?, stage=?, updated_at=?,
                   started_at=COALESCE(started_at, CASE WHEN ? NOT IN ('queued','pending') THEN ? END),
                   finished_at=COALESCE(finished_at, ?), duration_ms=COALESCE(duration_ms, ?),
                   input_quantity=CASE WHEN ?!='0' THEN ? ELSE input_quantity END,
                   unit_type=CASE WHEN ?!='task' THEN ? ELSE unit_type END,
                   result_summary=?, error_code=COALESCE(?, error_code) WHERE id=?""",
                (
                    status_value, stage, now, status_value, now, finished_at, duration_ms,
                    str(quantity), str(quantity), unit_type, unit_type,
                    json.dumps(summary, ensure_ascii=False), error_code(payload, 500 if status_value in TERMINAL_FAILURE else 200),
                    task["id"],
                ),
            )
            if task["status"] != status_value:
                connection.execute(
                    "INSERT INTO task_events(task_id, timestamp, level, stage, message, details_json) VALUES(?,?,?,?,?,?)",
                    (
                        task["id"], now, "error" if status_value in TERMINAL_FAILURE else "info",
                        stage, f"后台同步任务状态为 {status_value}",
                        json.dumps({"previous_status": task["status"], "status": status_value}, ensure_ascii=False),
                    ),
                )
            self._finalize_billing(connection, task["id"], task["first_call_id"] or "")
            connection.execute("COMMIT")

    def backfill_task_timings(self, service: str, rows: Iterable[dict[str, Any]]) -> int:
        updated = 0
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                external_id = str(row.get("external_task_id") or "")
                if not external_id or not row.get("finished_at") or not row.get("created_at"):
                    continue
                duration_ms = max(0.0, (
                    datetime.fromisoformat(str(row["finished_at"]))
                    - datetime.fromisoformat(str(row["created_at"]))
                ).total_seconds() * 1000)
                cursor = connection.execute(
                    """UPDATE tasks SET started_at=COALESCE(?,started_at),finished_at=?,duration_ms=?,
                       status=COALESCE(?,status),stage=COALESCE(?,stage),updated_at=MAX(updated_at,?)
                       WHERE service=? AND external_task_id=?""",
                    (
                        row.get("started_at"), row["finished_at"], duration_ms,
                        row.get("status"), row.get("stage"), row["finished_at"], service, external_id,
                    ),
                )
                updated += cursor.rowcount
            connection.execute("COMMIT")
        return updated

    def list_pricing_rules(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = self._rows(connection.execute(
                "SELECT * FROM pricing_rules ORDER BY service, operation, effective_from DESC"
            ).fetchall())
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        return rows

    def create_pricing_rule(self, payload: dict[str, Any], created_by: str = "admin") -> dict[str, Any]:
        service = str(payload["service"]).strip().lower()
        operation = str(payload.get("operation") or "*").strip()
        unit_type = str(payload["unit_type"]).strip()
        fixed_fee = Decimal(str(payload.get("fixed_fee", "0")))
        unit_price = Decimal(str(payload.get("unit_price", "0")))
        if service not in {"asr", "tts", "ocr", "subtitle", "lipsync", "face", "scene", "watermark", "depth", "upscale", "separation", "h3", "video_review"}:
            raise ValueError("unsupported service")
        if unit_type not in {"task", "audio_minute", "1000_chars", "image", "video_minute", "output_second"}:
            raise ValueError("unsupported unit type")
        if fixed_fee < 0 or unit_price < 0:
            raise ValueError("prices cannot be negative")
        effective = str(payload.get("effective_from") or iso_utc())
        datetime.fromisoformat(effective.replace("Z", "+00:00"))
        record = {
            "id": str(uuid4()), "service": service, "operation": operation,
            "unit_type": unit_type, "currency": "CNY", "fixed_fee": str(fixed_fee),
            "unit_price": str(unit_price), "effective_from": effective,
            "enabled": True, "created_at": iso_utc(), "created_by": created_by,
        }
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO pricing_rules(id, service, operation, unit_type, currency,
                   fixed_fee, unit_price, effective_from, enabled, created_at, created_by)
                   VALUES(:id,:service,:operation,:unit_type,:currency,:fixed_fee,:unit_price,
                   :effective_from,:enabled,:created_at,:created_by)""",
                record,
            )
        return record

    def cleanup(self) -> dict[str, int]:
        now = iso_utc()
        cutoff = iso_utc(utc_now() - timedelta(days=self.record_retention_days))
        with self._lock, self._connection() as connection:
            snapshots = connection.execute("DELETE FROM payload_snapshots WHERE expires_at<?", (now,)).rowcount
            calls = connection.execute("DELETE FROM api_calls WHERE started_at<?", (cutoff,)).rowcount
            tasks = connection.execute("DELETE FROM tasks WHERE created_at<?", (cutoff,)).rowcount
            connection.execute("PRAGMA optimize")
        backed_up = self.backup_daily()
        return {"snapshots": snapshots, "calls": calls, "tasks": tasks, "backup": int(backed_up)}

    def backup_daily(self) -> bool:
        day = datetime.now(SHANGHAI).date().isoformat()
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        destination = backup_dir / f"observability-{day}.db"
        if destination.exists():
            return False
        temporary = backup_dir / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            with self._lock, self._connection() as source:
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                finally:
                    target.close()
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
            backups = sorted(backup_dir.glob("observability-*.db"), reverse=True)
            for expired in backups[7:]:
                expired.unlink(missing_ok=True)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def health(self) -> dict[str, Any]:
        try:
            with self._lock, self._connection() as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                size = self.path.stat().st_size if self.path.exists() else 0
            disk = shutil.disk_usage(self.path.parent)
            disk_low = disk.free < 5 * 1024**3 or disk.free / disk.total < 0.05
            healthy = integrity == "ok" and not self._last_error and not disk_low
            return {
                "status": "ok" if healthy else "degraded",
                "integrity": integrity,
                "database_bytes": size,
                "disk_free_bytes": disk.free,
                "disk_low": disk_low,
                "dropped_events": self._dropped_events,
                "last_error": self._last_error,
            }
        except Exception as exc:
            return {"status": "error", "error": type(exc).__name__}

    def export_calls_csv(self, from_date: str | None, to_date: str | None, service: str | None) -> str:
        result = self.list_calls(page=1, page_size=10000, from_date=from_date, to_date=to_date, service=service)
        output = io.StringIO()
        fields = ["started_at", "trace_id", "service", "operation", "method", "path", "status_code", "duration_ms", "task_id", "error_code"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["items"])
        return output.getvalue()


class ObservabilityMiddleware:
    def __init__(self, app: Any, store_factory: Any) -> None:
        self.app = app
        self.store_factory = store_factory

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or route_info(scope.get("method", "GET"), scope.get("path", "")) is None:
            await self.app(scope, receive, send)
            return
        try:
            store = self.store_factory()
        except Exception:
            await self.app(scope, receive, send)
            return
        started = utc_now()
        call_id = str(uuid4())
        trace_id = self._header(scope, b"x-request-id") or str(uuid4())
        request_content_type = self._header(scope, b"content-type") or ""
        authorization = self._header(scope, b"authorization") or ""
        request_body = bytearray()
        request_bytes = 0
        response_body = bytearray()
        response_bytes = 0
        response_status_code = 500
        response_content_type = ""
        response_headers: dict[str, str] = {}
        final_body_sent = False

        async def capture_receive() -> dict[str, Any]:
            nonlocal request_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                request_bytes += len(chunk)
                if "json" in request_content_type.lower() and len(request_body) < MAX_SNAPSHOT_BYTES:
                    request_body.extend(chunk[: MAX_SNAPSHOT_BYTES - len(request_body)])
            return message

        async def capture_send(message: dict[str, Any]) -> None:
            nonlocal response_status_code, response_content_type, response_bytes, final_body_sent, response_headers
            if message["type"] == "http.response.start":
                response_status_code = int(message["status"])
                header_map = {
                    key.decode("latin1").lower(): value.decode("latin1")
                    for key, value in message.get("headers", [])
                }
                response_content_type = header_map.get("content-type", "")
                response_headers = {
                    key: value for key, value in header_map.items()
                    if key in {"x-audio-duration-ms", "x-tts-request-id", "x-ocr-worker"}
                }
                mutable = list(message.get("headers", []))
                if not any(key.lower() == b"x-request-id" for key, _ in mutable):
                    mutable.append((b"x-request-id", trace_id.encode("ascii")))
                message["headers"] = mutable
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                response_bytes += len(chunk)
                if "json" in response_content_type.lower() and len(response_body) < MAX_SNAPSHOT_BYTES:
                    response_body.extend(chunk[: MAX_SNAPSHOT_BYTES - len(response_body)])
                if not message.get("more_body", False):
                    final_body_sent = True
            await send(message)

        try:
            await self.app(scope, capture_receive, capture_send)
        finally:
            finished = utc_now()
            client = scope.get("client") or ("unknown", 0)
            forwarded = self._header(scope, b"x-forwarded-for")
            captured = CapturedCall(
                call_id=call_id,
                trace_id=trace_id,
                started_at=iso_utc(started),
                method=scope.get("method", "GET"),
                path=scope.get("path", ""),
                query=scope.get("query_string", b"").decode("utf-8", errors="ignore"),
                client_address=(forwarded.split(",", 1)[0].strip() if forwarded else str(client[0])),
                token_fingerprint=store.token_fingerprint(authorization),
                request_content_type=request_content_type,
                request_body=bytes(request_body),
                request_bytes=request_bytes,
                status_code=response_status_code,
                response_content_type=response_content_type,
                response_headers=response_headers,
                response_body=bytes(response_body),
                response_bytes=response_bytes,
                finished_at=iso_utc(finished),
                duration_ms=(finished - started).total_seconds() * 1000,
                disconnected=not final_body_sent,
            )
            try:
                import asyncio
                if not self._header(scope, b"x-ai-centre-probe"):
                    await asyncio.to_thread(store.safe_record, captured)
            except Exception:
                # Observability must never make a production inference request fail.
                pass

    @staticmethod
    def _header(scope: dict[str, Any], name: bytes) -> str | None:
        for key, value in scope.get("headers", []):
            if key.lower() == name:
                return value.decode("latin1")
        return None
