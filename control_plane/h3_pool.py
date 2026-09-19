from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis

from .config import Settings
from .h3_alerts import H3LarkWebhook, safe_alert_error
from .h3_store import H3Store, utc_now
from .h3_suanli import MACHINE_PROFILES, PROVIDER_MODES, SuanliClient, SuanliError


def _age_seconds(value: str | None) -> float:
    if not value:
        return 0
    return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds())


class H3PoolManager:
    def __init__(self, settings: Settings, store: H3Store) -> None:
        self.settings = settings
        self.store = store
        self.redis = redis.Redis.from_url(settings.redis_result_url, decode_responses=True)

    @property
    def startup_grace_seconds(self) -> int:
        return int(getattr(self.settings, "h3_pool_startup_grace_seconds", 1200))

    @property
    def offline_cleanup_seconds(self) -> int:
        return int(getattr(self.settings, "h3_worker_offline_cleanup_seconds", 1800))

    def _client(self) -> SuanliClient:
        return SuanliClient(self.settings.suanli_token or "", self.settings.suanli_base_url)

    def _notifier(self) -> H3LarkWebhook:
        return H3LarkWebhook(self.settings)

    @staticmethod
    def _healthy(worker: dict[str, Any]) -> bool:
        return bool(
            worker.get("enabled") and not worker.get("draining")
            and worker.get("status") in {"online", "busy"}
        )

    def _worker_for(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if not item.get("worker_id"):
            return None
        try:
            return self.store.worker(str(item["worker_id"]), public=False)
        except Exception:
            return None

    def _pending_within_grace(self, item: dict[str, Any]) -> bool:
        within_grace = _age_seconds(str(item.get("created_at") or "")) <= self.startup_grace_seconds
        if item.get("status") in {"requested", "provisioning"}:
            return within_grace
        worker = self._worker_for(item)
        return bool(
            within_grace and item.get("status") == "active" and worker
            and worker.get("status") == "unknown"
        )

    def capacity_count(self) -> int:
        healthy = sum(self._healthy(worker) for worker in self.store.list_workers(public=False))
        pending = self.warming_capacity_count()
        return int(healthy + pending)

    def warming_capacity_count(self) -> int:
        """Return provider slots that are expected to become schedulable soon.

        A warming slot satisfies the one-idle-worker reserve while it is still
        inside the startup grace period.  Without this guard, the 10-second
        reconciler would request another paid worker on every pass until the
        pool reached ``max_workers``.
        """
        pending = 0
        for item in self.store.list_managed_deployments(active_only=True):
            worker = self._worker_for(item)
            if self._pending_within_grace(item) and not self._healthy(worker or {}):
                pending += 1
        return pending

    def provider_slot_count(self) -> int:
        deployments = self.store.list_managed_deployments(active_only=True)
        linked = {str(item["worker_id"]) for item in deployments if item.get("worker_id")}
        unmanaged = sum(
            self._healthy(worker) and str(worker["id"]) not in linked
            for worker in self.store.list_workers(public=False)
        )
        return len(deployments) + int(unmanaged)

    def request_worker(
        self,
        machine_type: str,
        *,
        provider_mode: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        if machine_type not in MACHINE_PROFILES:
            raise ValueError("machine_type must be 4090_24g, 4090_48g or 5090_32g")
        mode = provider_mode or str(self.store.pool_config()["provider_mode"])
        if mode not in PROVIDER_MODES:
            raise ValueError("provider_mode must be spot or deployment")
        if self.provider_slot_count() >= int(self.store.pool_config()["max_workers"]):
            raise ValueError("worker pool has reached max_workers")
        allowed, retry_at = self.store.provisioning_allowed(machine_type)
        if not allowed:
            raise ValueError(f"worker provisioning is cooling down until {retry_at}")
        return self.store.request_managed_worker(machine_type, provider_mode=mode, name=name)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, SuanliError) and exc.code:
            return exc.code
        matched = re.match(r"^([A-Z]\d{3,})\s*:", str(exc))
        return matched.group(1) if matched else type(exc).__name__

    def _send_failure_alert(self, incident: dict[str, Any]) -> None:
        if int(incident["consecutive_failures"]) < 3 or incident.get("alerted_at"):
            return
        notifier = self._notifier()
        if not notifier.configured:
            self.store.record_pool_notification(
                str(incident["id"]), status="disabled", error="飞书群机器人Webhook未配置"
            )
            return
        summary = self.store.worker_summary()
        code = str(incident["id"])[:8]
        message = (
            f"【AI Centre H3 扩容告警】#{code}\n"
            f"机型：{incident['machine_type']}\n"
            f"错误：{incident['error_code']} · {safe_alert_error(str(incident['last_error']))}\n"
            f"连续失败：{incident['consecutive_failures']} 次\n"
            f"可用/忙碌/排队：{summary['online']}/{summary['busy']}/{self.store.pending_job_count()}\n"
            f"首次失败：{incident['first_failure_at']}"
        )
        try:
            notifier.send(message)
            self.store.record_pool_notification(str(incident["id"]), status="alert_sent", alerted=True)
        except Exception as exc:
            self.store.record_pool_notification(
                str(incident["id"]), status="alert_failed", error=safe_alert_error(str(exc))
            )

    def _record_provision_failure(self, item: dict[str, Any], exc: Exception) -> dict[str, Any]:
        safe_error = f"{type(exc).__name__}: {safe_alert_error(str(exc))}"
        incident = self.store.record_pool_failure(
            str(item["machine_type"]), self._error_code(exc), safe_error,
            self.capacity_count(),
            balance_cooldown_seconds=int(getattr(self.settings, "h3_pool_balance_cooldown_seconds", 1800)),
            other_backoff_max_seconds=int(getattr(self.settings, "h3_pool_other_backoff_max_seconds", 600)),
        )
        self.store.update_managed_deployment(str(item["id"]), status="failed", last_error=safe_error)
        self._send_failure_alert(incident)
        return incident

    def _provision_requested(self, item: dict[str, Any], client: SuanliClient) -> None:
        allowed, _retry_at = self.store.provisioning_allowed(str(item["machine_type"]))
        if not allowed:
            return
        try:
            config = self.store.pool_config()
            task_id = client.create_h3_worker(
                str(item["name"]),
                str(item["machine_type"]),
                provider_mode=str(item.get("provider_mode") or "deployment"),
                spot_estimated_exec_seconds=int(config["spot_estimated_exec_seconds"]),
            )
            self.store.update_managed_deployment(
                str(item["id"]), provider_task_id=task_id, status="provisioning", last_error=None
            )
        except Exception as exc:
            self._record_provision_failure(item, exc)

    def _refresh_deployment(self, item: dict[str, Any], client: SuanliClient) -> dict[str, Any]:
        if not item.get("provider_task_id"):
            return item
        try:
            provider_mode = str(item.get("provider_mode") or "deployment")
            detail = client.detail(int(item["provider_task_id"]), provider_mode=provider_mode)
            provider_status = str(detail.get("status") or "unknown")
            job_status = detail.get("job_status") or {}
            job_status = str(job_status.get("status") or "") if isinstance(job_status, dict) else str(job_status)
            if provider_status == "End" or job_status == "Complete":
                return self.store.update_managed_deployment(str(item["id"]), status="stopped", idle_since=None)
            if job_status == "Failed":
                return self.store.update_managed_deployment(
                    str(item["id"]), status="failed", idle_since=None,
                    last_error="provider Spot job failed",
                )
            url = client.worker_url(detail)
            if provider_status == "Running" and url and not item.get("worker_id"):
                worker = self.store.create_worker(str(item["name"]), url, True)
                return self.store.update_managed_deployment(
                    str(item["id"]), worker_id=worker["id"], worker_url=url,
                    status="active", last_error=None,
                )
            if provider_status == "Running" and item.get("worker_id"):
                return self.store.update_managed_deployment(str(item["id"]), status="active", last_error=None)
            if provider_status in {"Pending", "Running"}:
                return self.store.update_managed_deployment(str(item["id"]), status="provisioning", last_error=None)
            return self.store.update_managed_deployment(
                str(item["id"]), status="failed", last_error=f"provider status: {provider_status}"
            )
        except Exception as exc:
            return self.store.update_managed_deployment(
                str(item["id"]), last_error=f"{type(exc).__name__}: provider refresh failed"
            )

    def _stale(self, item: dict[str, Any]) -> bool:
        if item.get("status") == "unavailable":
            return True
        if item.get("status") != "active":
            return False
        worker = self._worker_for(item)
        if not worker:
            return _age_seconds(str(item.get("created_at") or "")) > self.startup_grace_seconds
        if worker.get("status") == "incompatible":
            return _age_seconds(str(item.get("created_at") or "")) > self.startup_grace_seconds
        return bool(
            worker.get("status") == "offline"
            and int(worker.get("failure_count") or 0) >= 3
            and _age_seconds(str(worker.get("offline_since") or "")) >= self.offline_cleanup_seconds
        )

    def _retire_stale(self, item: dict[str, Any], client: SuanliClient) -> dict[str, Any]:
        refreshed = self._refresh_deployment(item, client)
        if refreshed.get("status") in {"stopped", "failed"}:
            return refreshed
        worker = self._worker_for(refreshed)
        if worker and worker.get("current_job_id"):
            return self.store.update_managed_deployment(
                str(refreshed["id"]), status="unavailable",
                last_error="worker offline; waiting for active job failover",
            )
        try:
            if refreshed.get("provider_task_id"):
                client.stop(
                    int(refreshed["provider_task_id"]),
                    provider_mode=str(refreshed.get("provider_mode") or "deployment"),
                )
            if worker:
                self.store.delete_worker(str(worker["id"]))
            return self.store.update_managed_deployment(
                str(refreshed["id"]), status="stopped", idle_since=None,
                last_error="worker health check failed; instance replaced",
            )
        except Exception as exc:
            return self.store.update_managed_deployment(
                str(refreshed["id"]), status="unavailable",
                last_error=f"{type(exc).__name__}: stale worker cleanup failed",
            )

    def _update_idle(self, item: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker_for(item)
        if not worker:
            return item
        idle = (
            self._healthy(worker) and worker["status"] == "online"
            and not worker["current_job_id"] and not worker["queue_running"] and not worker["queue_pending"]
        )
        if idle and not item.get("idle_since"):
            return self.store.update_managed_deployment(str(item["id"]), idle_since=utc_now())
        if not idle and item.get("idle_since"):
            return self.store.update_managed_deployment(str(item["id"]), idle_since=None)
        return item

    def retire(self, deployment_id: str) -> None:
        item = self.store.managed_deployment(deployment_id)
        if item.get("worker_id"):
            worker = self.store.worker(str(item["worker_id"]), public=False)
            if worker.get("current_job_id"):
                raise ValueError("busy worker cannot be retired")
            self.store.update_worker(str(item["worker_id"]), {"enabled": False, "draining": True})
        if item.get("provider_task_id"):
            with self._client() as client:
                client.stop(
                    int(item["provider_task_id"]),
                    provider_mode=str(item.get("provider_mode") or "deployment"),
                )
        if item.get("worker_id"):
            self.store.delete_worker(str(item["worker_id"]))
        self.store.update_managed_deployment(deployment_id, status="stopped", idle_since=None)

    def _recover_incidents(self, config: dict[str, Any]) -> None:
        capacity = self.capacity_count()
        notifier = self._notifier()
        for incident in self.store.active_pool_incidents():
            recovered = capacity > int(incident.get("capacity_at_failure") or 0)
            recovered = recovered or capacity >= max(1, int(config["min_workers"]))
            if not recovered:
                continue
            status = "resolved_without_alert"
            if incident.get("notification_status") == "alert_sent":
                code = str(incident["id"])[:8]
                try:
                    notifier.send(
                        f"【AI Centre H3 扩容已恢复】#{code}\n"
                        f"机型：{incident['machine_type']}\n当前可用Worker：{capacity}\n调度池已恢复接单。"
                    )
                    status = "recovery_sent"
                except Exception as exc:
                    status = "recovery_failed"
                    self.store.record_pool_notification(
                        str(incident["id"]), status=status, error=safe_alert_error(str(exc))
                    )
            self.store.resolve_pool_incident(str(incident["id"]), notification_status=status)

    def reconcile(self) -> dict[str, Any]:
        if not self.settings.suanli_token:
            return {"configured": False, "reason": "SUANLI_TOKEN is not configured"}
        lock_key = "ai-centre2:h3:pool:reconcile"
        lock_value = uuid4().hex
        if not self.redis.set(lock_key, lock_value, nx=True, ex=25):
            return {"configured": True, "skipped": "locked"}
        config = self.store.pool_config()
        try:
            with self._client() as client:
                requested = [
                    item for item in self.store.list_managed_deployments(active_only=True)
                    if item["status"] == "requested"
                ]
                if requested:
                    self._provision_requested(requested[-1], client)
                for item in self.store.list_managed_deployments(active_only=True):
                    if item["status"] == "provisioning":
                        refreshed = self._refresh_deployment(item, client)
                        if (
                            refreshed.get("status") == "provisioning"
                            and _age_seconds(str(item.get("created_at") or "")) > self.startup_grace_seconds
                        ):
                            self._retire_stale(refreshed, client)
                for item in self.store.list_managed_deployments(active_only=True):
                    if self._stale(item):
                        self._retire_stale(item, client)
                self.store.cleanup_offline_workers(self.offline_cleanup_seconds)

            active = [
                self._update_idle(item)
                for item in self.store.list_managed_deployments(active_only=True)
                if item["status"] == "active" and self._healthy(self._worker_for(item) or {})
            ]
            capacity = self.capacity_count()
            provider_slots = self.provider_slot_count()
            if config["autoscaling_enabled"]:
                max_workers = int(config["max_workers"])
                # Automatic mode always keeps one immediately schedulable
                # worker.  A requested/provisioning worker counts as the future
                # reserve so that only one machine is started at a time.
                effective_min_workers = max(1, int(config["min_workers"]))
                idle_workers = self.store.schedulable_workers()
                warming_capacity = self.warming_capacity_count()
                should_add = capacity < effective_min_workers
                if not idle_workers and warming_capacity == 0:
                    should_add = True
                if should_add and provider_slots < max_workers:
                    machine_type = str(config["default_machine_type"])
                    allowed, _retry_at = self.store.provisioning_allowed(machine_type)
                    if allowed:
                        self.request_worker(machine_type, provider_mode=str(config["provider_mode"]))
                elif capacity > effective_min_workers and len(idle_workers) > 1:
                    candidates = [
                        item for item in active if item.get("idle_since")
                        and _age_seconds(str(item["idle_since"])) >= int(config["idle_timeout_seconds"])
                    ]
                    if candidates:
                        self.retire(sorted(candidates, key=lambda item: item["idle_since"])[0]["id"])
            self._recover_incidents(config)
            return {
                "configured": True, "capacity": self.capacity_count(),
                "provider_slots": self.provider_slot_count(),
                "pending_jobs": self.store.pending_job_count(),
            }
        except Exception as exc:
            return {"configured": True, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self.redis.eval(
                "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) end return 0",
                1, lock_key, lock_value,
            )

    def _deployment_available(self, item: dict[str, Any]) -> bool:
        if self._pending_within_grace(item):
            return True
        return bool(item.get("status") == "active" and self._healthy(self._worker_for(item) or {}))

    def _deployment_view(self, item: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker_for(item)
        view = dict(item)
        view.pop("worker_url", None)
        view["available"] = self._deployment_available(item)
        view["worker_status"] = worker.get("status") if worker else None
        view["offline_duration_seconds"] = worker.get("offline_duration_seconds") if worker else None
        return view

    def page_deployments(
        self, *, availability: str = "unavailable", page: int = 1,
        page_size: int = 20, status: str | None = None,
    ) -> dict[str, Any]:
        rows = [self._deployment_view(item) for item in self.store.list_managed_deployments()]
        expected = availability == "available"
        rows = [item for item in rows if bool(item["available"]) == expected]
        if status:
            rows = [item for item in rows if item["status"] == status]
        total = len(rows)
        start = (page - 1) * page_size
        return {"items": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size}

    def status(self) -> dict[str, Any]:
        deployments = [self._deployment_view(item) for item in self.store.list_managed_deployments()]
        available = [item for item in deployments if item["available"]]
        unavailable = [item for item in deployments if not item["available"]]
        counts: dict[str, int] = {}
        for item in unavailable:
            key = str(item["status"])
            counts[key] = counts.get(key, 0) + 1
        notifier = self._notifier()
        incidents = self.store.active_pool_incidents()
        return {
            "configured": bool(self.settings.suanli_token),
            "config": self.store.pool_config(),
            "capacity": self.capacity_count(),
            "provider_slots": self.provider_slot_count(),
            "pending_jobs": self.store.pending_job_count(),
            "deployments": available,
            "unavailable_total": len(unavailable),
            "unavailable_counts": counts,
            "incidents": incidents[:20],
            "notifications": {
                "configured": notifier.configured, "channel": "lark_webhook",
                "last_status": incidents[0].get("notification_status") if incidents else None,
                "last_at": incidents[0].get("notification_at") if incidents else None,
                "last_error": incidents[0].get("notification_error") if incidents else None,
            },
            "performance": self.store.performance_summary(),
            "machine_types": [
                {"value": "4090_24g", "label": "RTX 4090 · 24G"},
                {"value": "4090_48g", "label": "RTX 4090 · 48G"},
                {"value": "5090_32g", "label": "RTX 5090 · 32G"},
            ],
            "provider_modes": [
                {"value": "spot", "label": "抢占式 Spot Job"},
                {"value": "deployment", "label": "弹性部署 Deployment"},
            ],
        }
