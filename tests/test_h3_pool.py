from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from control_plane.h3_pool import H3PoolManager
from control_plane.h3_store import H3Store
from control_plane.h3_suanli import SuanliError


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, _script: str, _keys: int, key: str, value: str):
        if self.values.get(key) == value:
            self.values.pop(key, None)
            return 1
        return 0


@pytest.fixture()
def manager(tmp_path: Path) -> H3PoolManager:
    settings = SimpleNamespace(
        redis_result_url="redis://unused/0",
        suanli_token="test-token",
        suanli_base_url="https://openapi.suanli.cn",
    )
    instance = H3PoolManager.__new__(H3PoolManager)
    instance.settings = settings
    instance.store = H3Store(tmp_path / "h3.db")
    instance.redis = FakeRedis()
    return instance


def test_machine_types_and_maximum_capacity(manager: H3PoolManager) -> None:
    for machine_type in ("4090_24g", "4090_48g", "5090_32g"):
        assert manager.request_worker(machine_type)["machine_type"] == machine_type
    manager.store.update_pool_config({"min_workers": 0, "max_workers": 3})
    with pytest.raises(ValueError, match="max_workers"):
        manager.request_worker("5090_32g")
    with pytest.raises(ValueError, match="machine_type"):
        manager.request_worker("a100")


def test_reconcile_adds_one_worker_to_meet_minimum(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 1, "max_workers": 3})
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    result = manager.reconcile()

    assert result["capacity"] == 1
    requested = manager.store.list_managed_deployments(active_only=True)
    assert len(requested) == 1
    assert requested[0]["machine_type"] == "5090_32g"


def test_all_busy_workers_scale_up_before_a_queue_forms(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 0, "max_workers": 2})
    worker = manager.store.create_worker("busy", "https://busy.example.com")
    manager.store.record_probe(worker["id"], ok=True)
    manager.store.record_probe(worker["id"], ok=True, queue_running=1)
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    manager.reconcile()

    assert manager.capacity_count() == 2
    assert manager.store.pending_job_count() == 0
    assert len(manager.store.list_managed_deployments(active_only=True)) == 1


def test_idle_worker_does_not_scale_up(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 0, "max_workers": 2})
    worker = manager.store.create_worker("idle", "https://idle.example.com")
    manager.store.record_probe(worker["id"], ok=True)
    manager.store.record_probe(worker["id"], ok=True)
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    manager.reconcile()

    assert manager.capacity_count() == 1
    assert manager.store.list_managed_deployments(active_only=True) == []


def test_warming_worker_prevents_duplicate_reserve_scale_up(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 0, "max_workers": 3})
    worker = manager.store.create_worker("busy", "https://busy.example.com")
    manager.store.record_probe(worker["id"], ok=True)
    manager.store.record_probe(worker["id"], ok=True, queue_running=1)
    manager.store.request_managed_worker("5090_32g", name="warming-reserve")
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.create_h3_worker.return_value = 202
    client.detail.return_value = {"status": "Pending"}
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    manager.reconcile()

    deployments = manager.store.list_managed_deployments(active_only=True)
    assert len(deployments) == 1
    assert deployments[0]["status"] == "provisioning"
    assert manager.capacity_count() == 2


def test_all_busy_workers_do_not_scale_beyond_maximum(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 0, "max_workers": 1})
    worker = manager.store.create_worker("busy", "https://busy.example.com")
    manager.store.record_probe(worker["id"], ok=True)
    manager.store.record_probe(worker["id"], ok=True, queue_running=1)
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    result = manager.reconcile()

    assert result["capacity"] == 1
    assert result["provider_slots"] == 1
    assert manager.store.list_managed_deployments(active_only=True) == []


def test_new_active_worker_waiting_for_health_does_not_trigger_duplicate_scale(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 1, "max_workers": 3})
    worker = manager.store.create_worker("warming", "https://warming.example.com")
    deployment = manager.store.request_managed_worker("5090_32g", name="warming-deployment")
    manager.store.update_managed_deployment(
        deployment["id"], provider_task_id=100, worker_id=worker["id"], status="active"
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    result = manager.reconcile()

    assert result["capacity"] == 1
    assert result["provider_slots"] == 1
    assert len(manager.store.list_managed_deployments(active_only=True)) == 1


def test_idle_worker_is_retired_but_never_below_minimum(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 1, "max_workers": 3, "idle_timeout_seconds": 60})
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    first = manager.store.request_managed_worker("5090_32g", name="first")
    second = manager.store.request_managed_worker("5090_32g", name="second")
    first_worker = manager.store.create_worker("first-worker", "https://first.example.com")
    second_worker = manager.store.create_worker("second-worker", "https://second.example.com")
    for worker in (first_worker, second_worker):
        manager.store.record_probe(worker["id"], ok=True)
        manager.store.record_probe(worker["id"], ok=True)
    manager.store.update_managed_deployment(first["id"], worker_id=first_worker["id"], status="active", idle_since=old)
    manager.store.update_managed_deployment(second["id"], worker_id=second_worker["id"], status="active", idle_since=old)
    manager._update_idle = MagicMock(side_effect=lambda item: item)  # type: ignore[method-assign]
    manager.retire = MagicMock()  # type: ignore[method-assign]
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    manager.reconcile()

    manager.retire.assert_called_once()


def test_only_idle_reserve_is_not_retired_while_other_workers_are_busy(manager: H3PoolManager) -> None:
    manager.store.update_pool_config({"min_workers": 1, "max_workers": 3, "idle_timeout_seconds": 0})
    idle_deployment = manager.store.request_managed_worker("5090_32g", name="idle")
    idle_worker = manager.store.create_worker("idle-worker", "https://idle.example.com")
    busy_worker = manager.store.create_worker("busy-worker", "https://busy.example.com")
    for worker in (idle_worker, busy_worker):
        manager.store.record_probe(worker["id"], ok=True)
        manager.store.record_probe(worker["id"], ok=True)
    manager.store.record_probe(busy_worker["id"], ok=True, queue_running=1)
    manager.store.update_managed_deployment(
        idle_deployment["id"], worker_id=idle_worker["id"], status="active",
        idle_since=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    manager._update_idle = MagicMock(side_effect=lambda item: item)  # type: ignore[method-assign]
    manager.retire = MagicMock()  # type: ignore[method-assign]
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    manager.reconcile()

    manager.retire.assert_not_called()


def test_offline_active_deployment_is_reaped_and_replaced(manager: H3PoolManager) -> None:
    manager.settings.h3_pool_startup_grace_seconds = 0
    manager.settings.h3_worker_offline_cleanup_seconds = 0
    manager.store.update_pool_config({"min_workers": 1, "max_workers": 2})
    worker = manager.store.create_worker("stale-worker", "https://stale.example.com")
    for _ in range(3):
        manager.store.record_probe(worker["id"], ok=False, error="offline")
    deployment = manager.store.request_managed_worker("5090_32g", name="stale")
    manager.store.update_managed_deployment(
        deployment["id"], provider_task_id=101, worker_id=worker["id"], status="active"
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.detail.return_value = {
        "status": "Running",
        "services": [{"remote_ports": [{"service_port": 8188, "url": "https://stale.example.com"}]}],
    }
    manager._client = MagicMock(return_value=client)  # type: ignore[method-assign]

    result = manager.reconcile()

    client.stop.assert_called_once_with(101, provider_mode="spot")
    assert manager.store.managed_deployment(deployment["id"])["status"] == "stopped"
    assert result["provider_slots"] == 1
    assert any(item["status"] == "requested" for item in manager.store.list_managed_deployments(active_only=True))


def test_offline_duration_is_reported_and_unmanaged_worker_is_cleaned(manager: H3PoolManager) -> None:
    worker = manager.store.create_worker("offline-worker", "https://offline.example.com")
    for _ in range(3):
        worker = manager.store.record_probe(worker["id"], ok=False, error="offline")

    assert worker["status"] == "offline"
    assert worker["offline_since"]
    assert worker["offline_duration_seconds"] is not None
    assert manager.store.cleanup_offline_workers(0) == [worker["id"]]
    assert manager.store.list_workers(public=False) == []


def test_balance_failure_uses_thirty_minute_cooldown(manager: H3PoolManager) -> None:
    item = manager.store.request_managed_worker("5090_32g")
    client = MagicMock()
    client.create_h3_worker.side_effect = SuanliError("C004: 余额不足", code="C004")

    manager._provision_requested(item, client)

    incident = manager.store.active_pool_incidents("5090_32g")[0]
    delay = datetime.fromisoformat(incident["next_retry_at"]) - datetime.fromisoformat(incident["last_failure_at"])
    assert delay.total_seconds() == 1800
    assert manager.store.provisioning_allowed("5090_32g")[0] is False
    assert manager.store.managed_deployment(item["id"])["status"] == "failed"


def test_third_failure_alerts_once_and_online_worker_reports_recovery(manager: H3PoolManager) -> None:
    class FakeNotifier:
        configured = True

        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str) -> None:
            self.messages.append(message)

    notifier = FakeNotifier()
    manager._notifier = MagicMock(return_value=notifier)  # type: ignore[method-assign]
    started = datetime(2026, 9, 4, tzinfo=timezone.utc)
    incident = None
    for index in range(3):
        incident = manager.store.record_pool_failure(
            "5090_32g", "C004", "SuanliError: C004: 余额不足", 0,
            balance_cooldown_seconds=1800, other_backoff_max_seconds=600,
            now=started + timedelta(minutes=30 * index),
        )
        manager._send_failure_alert(incident)

    assert len(notifier.messages) == 1
    assert "扩容告警" in notifier.messages[0]
    assert manager.store.active_pool_incidents()[0]["notification_status"] == "alert_sent"

    worker = manager.store.create_worker("recovered", "https://recovered.example.com")
    manager.store.record_probe(worker["id"], ok=True)
    manager.store.record_probe(worker["id"], ok=True)
    manager._recover_incidents(manager.store.pool_config())

    assert len(notifier.messages) == 2
    assert "已恢复" in notifier.messages[1]
    assert manager.store.active_pool_incidents() == []


def test_reconcile_lock_prevents_duplicate_scaling(manager: H3PoolManager) -> None:
    manager.redis.values["ai-centre2:h3:pool:reconcile"] = "other"
    assert manager.reconcile() == {"configured": True, "skipped": "locked"}
    assert manager.capacity_count() == 0
