from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .h3_alerts import H3LarkWebhook, safe_alert_error

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")

DEFAULT_TARGETS = (
    ("control", "控制面", "http://127.0.0.1:8320/health"),
    ("asr", "语音识别", "http://127.0.0.1:9001/health"),
    ("tts", "语音合成", "http://127.0.0.1:8193/health"),
    ("voxcpm2", "VoxCPM2克隆", "http://127.0.0.1:8192/health"),
    ("ocr", "OCR文字识别", "http://127.0.0.1:8096/health"),
    ("subtitle", "字幕检测", "http://127.0.0.1:8098/health"),
    ("face", "人脸处理", "http://127.0.0.1:8310/health"),
    ("musetalk", "唇形驱动/GFPGAN", "http://127.0.0.1:9011/health"),
    ("speaker", "说话人检测", "http://127.0.0.1:8195/health"),
    ("emotion", "情绪检测", "http://127.0.0.1:8196/health"),
    ("scene", "视频切片", "http://127.0.0.1:8320/health"),
    ("depth", "视频深度", "http://127.0.0.1:8320/health"),
    ("separation", "音频分离", "http://127.0.0.1:8320/health"),
    ("watermark", "水印处理", "http://127.0.0.1:8320/health"),
    ("upscale", "视频超分", "http://127.0.0.1:8320/health"),
    ("h3", "MiniMax H3", "http://127.0.0.1:8320/health"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * p + 0.999999)) - 1))
    return round(ordered[index], 2)


class HealthMonitorStore:
    def __init__(self, path: Path, secret: str, targets: tuple[tuple[str, str, str], ...] = DEFAULT_TARGETS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = hashlib.sha256(secret.encode()).digest()
        self._init(targets)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _init(self, targets: tuple[tuple[str, str, str], ...]) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS health_targets (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, paid INTEGER NOT NULL DEFAULT 0,
              current_status TEXT NOT NULL DEFAULT 'unknown', consecutive_failures INTEGER NOT NULL DEFAULT 0,
              consecutive_successes INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT,
              last_latency_ms REAL, last_error TEXT, last_l2_status TEXT, last_l2_at TEXT
            );
            CREATE TABLE IF NOT EXISTS health_checks (
              id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL, level TEXT NOT NULL,
              started_at TEXT NOT NULL, success INTEGER, latency_ms REAL, status_code INTEGER,
              error_type TEXT, summary TEXT, skipped_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_health_checks_target_time ON health_checks(target_id, started_at);
            CREATE TABLE IF NOT EXISTS health_incidents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL, status TEXT NOT NULL,
              started_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, recovered_at TEXT,
              last_notified_at TEXT, failure_summary TEXT
            );
            CREATE TABLE IF NOT EXISTS notification_config (
              id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
              webhook_cipher TEXT, secret_cipher TEXT, webhook_host TEXT,
              updated_at TEXT, last_delivery_at TEXT, last_delivery_status TEXT
            );
            CREATE TABLE IF NOT EXISTS notification_deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, kind TEXT NOT NULL,
              created_at TEXT NOT NULL, success INTEGER NOT NULL, error TEXT
            );
            INSERT OR IGNORE INTO notification_config(id, enabled) VALUES(1, 0);
            """)
            db.executemany(
                "INSERT INTO health_targets(id,name,url) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url",
                targets,
            )

    def _encrypt(self, value: str) -> str:
        nonce = os.urandom(12)
        raw = nonce + AESGCM(self.key).encrypt(nonce, value.encode(), b"ai-centre-health-v1")
        return base64.urlsafe_b64encode(raw).decode()

    def _decrypt(self, value: str | None) -> str:
        if not value:
            return ""
        raw = base64.urlsafe_b64decode(value)
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], b"ai-centre-health-v1").decode()

    def targets(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM health_targets ORDER BY name")]

    def public_config(self) -> dict[str, Any]:
        with self.connect() as db:
            row = dict(db.execute("SELECT * FROM notification_config WHERE id=1").fetchone())
        return {"enabled": bool(row["enabled"]), "configured": bool(row["webhook_cipher"]),
                "webhook_host": row["webhook_host"], "updated_at": row["updated_at"],
                "last_delivery_at": row["last_delivery_at"], "last_delivery_status": row["last_delivery_status"]}

    def save_config(self, enabled: bool, webhook_url: str | None, secret: str | None, clear: bool = False, clear_secret: bool = False) -> dict[str, Any]:
        host = None
        if webhook_url:
            parsed = urlsplit(webhook_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("飞书Webhook必须是HTTPS URL")
            host = parsed.hostname
        with self.connect() as db:
            current = db.execute("SELECT * FROM notification_config WHERE id=1").fetchone()
            webhook_cipher = None if clear else (self._encrypt(webhook_url) if webhook_url else current["webhook_cipher"])
            secret_cipher = None if clear or clear_secret else (self._encrypt(secret) if secret else current["secret_cipher"])
            webhook_host = None if clear else (host or current["webhook_host"])
            db.execute("UPDATE notification_config SET enabled=?,webhook_cipher=?,secret_cipher=?,webhook_host=?,updated_at=? WHERE id=1",
                       (int(enabled and bool(webhook_cipher)), webhook_cipher, secret_cipher, webhook_host, _now()))
        return self.public_config()

    def notifier(self, settings: Any) -> H3LarkWebhook | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM notification_config WHERE id=1").fetchone()
        if not row or not row["enabled"] or not row["webhook_cipher"]:
            return None
        class RuntimeSettings:
            h3_lark_webhook_url = None
            h3_lark_webhook_secret = None
            h3_lark_timeout_seconds = settings.h3_lark_timeout_seconds
        from pydantic import SecretStr
        RuntimeSettings.h3_lark_webhook_url = SecretStr(self._decrypt(row["webhook_cipher"]))
        secret = self._decrypt(row["secret_cipher"])
        RuntimeSettings.h3_lark_webhook_secret = SecretStr(secret) if secret else None
        return H3LarkWebhook(RuntimeSettings())

    def record(self, target_id: str, level: str, success: bool | None, latency_ms: float | None,
               status_code: int | None = None, error: str | None = None, summary: str | None = None,
               skipped_reason: str | None = None, failure_threshold: int = 2, recovery_threshold: int = 2) -> dict[str, Any] | None:
        now = _now()
        transition = None
        with self.connect() as db:
            db.execute("INSERT INTO health_checks(target_id,level,started_at,success,latency_ms,status_code,error_type,summary,skipped_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                       (target_id, level, now, None if success is None else int(success), latency_ms, status_code,
                        safe_alert_error(error) if error else None, summary, skipped_reason))
            if success is None:
                return None
            row = db.execute("SELECT * FROM health_targets WHERE id=?", (target_id,)).fetchone()
            if row is None:
                return None
            failures = 0 if success else int(row["consecutive_failures"]) + 1
            successes = int(row["consecutive_successes"]) + 1 if success else 0
            old = row["current_status"]
            new = old
            if not success and failures >= failure_threshold: new = "major_outage"
            elif success and successes >= recovery_threshold: new = "operational"
            db.execute("UPDATE health_targets SET current_status=?,consecutive_failures=?,consecutive_successes=?,last_checked_at=?,last_latency_ms=?,last_error=?,last_l2_status=CASE WHEN ? IN ('l2','paid') THEN ? ELSE last_l2_status END,last_l2_at=CASE WHEN ? IN ('l2','paid') THEN ? ELSE last_l2_at END WHERE id=?",
                       (new, failures, successes, now, latency_ms, safe_alert_error(error) if error else None,
                        level, "ok" if success else "failed", level, now, target_id))
            if new != old and new == "major_outage":
                cursor = db.execute("INSERT INTO health_incidents(target_id,status,started_at,confirmed_at,failure_summary) VALUES(?,?,?,?,?)",
                                    (target_id, "open", now, now, safe_alert_error(error)))
                transition = {"kind": "failure", "incident_id": cursor.lastrowid, "target": row["name"], "error": safe_alert_error(error)}
            elif new != old and new == "operational":
                incident = db.execute("SELECT id,started_at FROM health_incidents WHERE target_id=? AND status='open' ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()
                if incident:
                    db.execute("UPDATE health_incidents SET status='recovered',recovered_at=? WHERE id=?", (now, incident["id"]))
                    transition = {"kind": "recovery", "incident_id": incident["id"], "target": row["name"], "started_at": incident["started_at"]}
        return transition

    def delivery(self, incident_id: int | None, kind: str, success: bool, error: str | None = None) -> None:
        now = _now()
        with self.connect() as db:
            db.execute("INSERT INTO notification_deliveries(incident_id,kind,created_at,success,error) VALUES(?,?,?,?,?)",
                       (incident_id, kind, now, int(success), safe_alert_error(error)))
            db.execute("UPDATE notification_config SET last_delivery_at=?,last_delivery_status=? WHERE id=1",
                       (now, "success" if success else "failed"))
            if incident_id and success:
                db.execute("UPDATE health_incidents SET last_notified_at=? WHERE id=?", (now, incident_id))

    def reminder_transitions(self) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with self.connect() as db:
            rows = db.execute("""SELECT i.id,i.target_id,i.started_at,i.failure_summary,t.name
              FROM health_incidents i JOIN health_targets t ON t.id=i.target_id
              WHERE i.status='open' AND COALESCE(i.last_notified_at,i.confirmed_at)<=?""", (cutoff,)).fetchall()
        return [{"kind":"reminder","incident_id":r["id"],"target":r["name"],"started_at":r["started_at"],"error":r["failure_summary"]} for r in rows]

    def status(self, days: int = 30) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        items = []
        with self.connect() as db:
            for target in db.execute("SELECT * FROM health_targets ORDER BY name"):
                checks = list(db.execute("SELECT * FROM health_checks WHERE target_id=? AND started_at>=? AND success IS NOT NULL ORDER BY started_at", (target["id"], since)))
                latencies = [float(x["latency_ms"]) for x in checks if x["latency_ms"] is not None]
                successes = sum(int(x["success"]) for x in checks)
                item = dict(target)
                item.update(sample_count=len(checks), availability=round(successes * 100 / len(checks), 3) if checks else None,
                            p50_ms=_percentile(latencies, .5), p95_ms=_percentile(latencies, .95),
                            history=[{"at": x["started_at"], "ok": bool(x["success"]), "level": x["level"]} for x in checks[-96:]])
                items.append(item)
            incidents = [dict(x) for x in db.execute("SELECT * FROM health_incidents ORDER BY id DESC LIMIT 50")]
        return {"days": days, "items": items, "incidents": incidents, "generated_at": _now()}


class HealthMonitor:
    def __init__(self, store: HealthMonitorStore, settings: Any) -> None:
        self.store, self.settings = store, settings

    def _notify(self, transition: dict[str, Any] | None) -> None:
        if not transition: return
        notifier = self.store.notifier(self.settings)
        if not notifier: return
        kind = transition["kind"]
        title = "故障告警" if kind == "failure" else ("持续故障提醒" if kind == "reminder" else "恢复通知")
        state = "连续2次鉴活失败" if kind == "failure" else ("故障已持续超过24小时" if kind == "reminder" else "连续2次鉴活成功，服务已恢复")
        template = "red" if kind == "failure" else ("orange" if kind == "reminder" else "green")
        icon = "🚨" if kind == "failure" else ("⏳" if kind == "reminder" else "✅")
        occurred_at = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        try:
            notifier.send_card(
                title=f"{icon} AI Centre {title}",
                template=template,
                fields=[
                    ("服务", transition["target"]),
                    ("当前状态", state),
                    ("发生时间", f"{occurred_at}（北京时间）"),
                    ("事件编号", str(transition.get("incident_id") or "—")),
                ],
                detail=transition.get("error"),
                button_url=self.settings.health_monitor_public_admin_url,
            ); self.store.delivery(transition.get("incident_id"), kind, True)
        except Exception as exc:
            self.store.delivery(transition.get("incident_id"), kind, False, str(exc))

    async def run_l1(self, target_id: str | None = None) -> dict[str, Any]:
        results = []
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            for target in self.store.targets():
                if not target["enabled"] or (target_id and target["id"] != target_id): continue
                started = time.perf_counter()
                code = None
                try:
                    response = await client.get(target["url"]); code = response.status_code
                    response.raise_for_status()
                    success, error = True, None
                except Exception as exc:
                    success, error = False, f"{type(exc).__name__}: {exc}"
                latency = round((time.perf_counter() - started) * 1000, 2)
                transition = self.store.record(target["id"], "l1", success, latency, code, error,
                                               failure_threshold=self.settings.health_monitor_failure_threshold,
                                               recovery_threshold=self.settings.health_monitor_recovery_threshold)
                self._notify(transition)
                results.append({"target": target["id"], "success": success, "latency_ms": latency, "status_code": code})
        for transition in self.store.reminder_transitions():
            self._notify(transition)
        return {"level": "l1", "results": results}

    async def run_l2(self, paid: bool = False) -> dict[str, Any]:
        """Run fixed, non-sensitive business probes through the public contract."""
        s = self.settings
        required = [s.health_monitor_probe_audio_url, s.health_monitor_probe_image_url,
                    s.health_monitor_probe_video_url, s.health_monitor_probe_face_video_url]
        if not all(required):
            return {"level": "paid" if paid else "l2", "status": "skipped", "reason": "未配置完整固定探针素材"}
        base = f"http://127.0.0.1:{s.control_port}"
        headers = {"authorization": f"Bearer {s.service_token}", "x-ai-centre-probe": "paid" if paid else "l2"}
        async with httpx.AsyncClient(timeout=30, trust_env=False, headers=headers) as client:
            if paid:
                specs = [
                    ("h3", "/v1/video-generations/minimax-h3/jobs", {"segment_mode":"single","prompts":["蓝色光点在白色背景上缓慢移动，固定镜头"],"duration_seconds":2,"resolution":"480p","aspect_ratio":"1:1","priority":1,"external_ref":"daily-health-probe","metadata":{"probe":True}}),
                    ("upscale", "/v1/video-upscale/jobs", {"source_uri":s.health_monitor_probe_video_url,"provider":"auto","max_resolution":480,"external_ref":"daily-health-probe","metadata":{"probe":True}}),
                ]
            else:
                specs = [
                    ("asr", "/v1/asr/transcriptions", {"file_url":s.health_monitor_probe_audio_url,"language":"auto","beam_size":1}),
                    ("tts", "/v2/tts/speech", {"text":"AI Centre 鉴活测试。","provider":"auto","voice_profile_id":"default"}),
                    ("ocr", "/v1/ocr/batch", {"job_id":"health-probe","source_lang_hint":"zh","images":[{"image_id":"probe","url":s.health_monitor_probe_image_url,"regions":[]}]}),
                    ("scene", "/v1/video-scenes/jobs", {"source_uri":s.health_monitor_probe_video_url,"filename":"probe-scene.mp4","metadata":{"probe":True}}),
                    ("watermark", "/v1/watermark-removal/jobs", {"source_uri":s.health_monitor_probe_video_url,"filename":"probe-watermark.mp4","mode":"light","metadata":{"probe":True}}),
                    ("depth", "/v1/video-depth/jobs", {"source_uri":s.health_monitor_probe_video_url,"version":"da2","model":"small","filename":"probe-depth.mp4","input_size":224,"max_resolution":480,"metadata":{"probe":True}}),
                    ("separation", "/v1/audio-separation/jobs", {"source_uri":s.health_monitor_probe_audio_url,"metadata":{"probe":True}}),
                    ("face", "/v1/face-mosaic/jobs", {"source_uri":s.health_monitor_probe_face_video_url,"filename":"probe-face.mp4","metadata":{"probe":True}}),
                    ("musetalk", "/v1/lipsync/jobs", {"video_url":s.health_monitor_probe_face_video_url,"audio_url":s.health_monitor_probe_audio_url,"face_restore":False}),
                ]
            results = []
            for target, path, payload in specs:
                started = time.perf_counter(); code = None; error = None
                try:
                    response = await client.post(base + path, json=payload); code = response.status_code; response.raise_for_status()
                    data = response.json() if "json" in response.headers.get("content-type", "") else {}
                    job_id = data.get("job_id")
                    if job_id and target not in {"asr", "tts", "ocr"}:
                        status_path = path + "/" + str(job_id)
                        deadline = time.monotonic() + (3600 if paid else 1800)
                        while time.monotonic() < deadline:
                            await __import__("asyncio").sleep(5)
                            state_response = await client.get(base + status_path); state_response.raise_for_status(); data = state_response.json()
                            state = str(data.get("status") or data.get("state") or "").lower()
                            if state in {"succeeded", "completed"}: break
                            if state in {"failed", "cancelled", "canceled"}: raise RuntimeError(data.get("error") or state)
                        else: raise TimeoutError("业务探针执行超时")
                    success = True
                except Exception as exc:
                    success = False; error = f"{type(exc).__name__}: {exc}"
                latency = round((time.perf_counter() - started) * 1000, 2)
                transition = self.store.record(target, "paid" if paid else "l2", success, latency, code, error,
                    failure_threshold=s.health_monitor_failure_threshold, recovery_threshold=s.health_monitor_recovery_threshold)
                self._notify(transition); results.append({"target":target,"success":success,"latency_ms":latency,"status_code":code})
            return {"level": "paid" if paid else "l2", "results": results}

    def test_notification(self) -> None:
        notifier = self.store.notifier(self.settings)
        if not notifier: raise RuntimeError("飞书群机器人Webhook未配置或未启用")
        notifier.send_card(
            title="🧪 AI Centre 通知测试",
            template="blue",
            fields=[
                ("状态", "飞书鉴活告警配置有效"),
                ("发送时间", f"{datetime.now(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}（北京时间）"),
            ],
            button_url=self.settings.health_monitor_public_admin_url,
        )
        self.store.delivery(None, "test", True)
