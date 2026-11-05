from pathlib import Path
import tempfile
from tempfile import TemporaryDirectory

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_plane import api
from control_plane.api_keys import ApiKeyStore
from control_plane.billing_api import router as billing_router
from control_plane.credit_pricing import Quote


def _client(store: ApiKeyStore, monkeypatch) -> TestClient:
    monkeypatch.setattr(api, "get_api_key_store", lambda: store)
    app = FastAPI()
    # 鉴权在 api.py 里由 include_router 统一挂，这里只测处理逻辑。
    app.include_router(billing_router)
    return TestClient(app)


@pytest.fixture()
def store(monkeypatch):
    with TemporaryDirectory() as directory:
        value = ApiKeyStore(Path(directory) / "keys.db")
        yield value


def test_settings_round_trip_in_yuan(store, monkeypatch) -> None:
    client = _client(store, monkeypatch)
    response = client.put("/internal/admin/billing/settings", json={"platform_limit_24h": 12.5})
    assert response.status_code == 200
    body = response.json()
    assert body["platform_limit_24h"] == 12.5
    assert body["platform_limit_24h_micro"] == 12_500_000

    assert client.put("/internal/admin/billing/settings", json={"platform_limit_24h": None}).json()[
        "platform_limit_24h"
    ] is None


def test_rates_upsert_and_delete(store, monkeypatch) -> None:
    client = _client(store, monkeypatch)
    response = client.put(
        "/internal/admin/billing/rates",
        json={"service": "video_generation", "model": "seedance-2.5", "unit": "second", "amount_yuan": 2.0},
    )
    assert response.status_code == 200
    row = next(item for item in response.json()["items"] if item["model"] == "seedance-2.5")
    assert row["amount_micro"] == 2_000_000
    # 改了费率，缓存要当场失效，否则后面的请求还按旧价算。
    assert any(r["amount_micro"] == 2_000_000 for r in store.rates_cached() if r["model"] == "seedance-2.5")

    deleted = client.delete("/internal/admin/billing/rates/video_generation/seedance-2.5/*")
    assert deleted.status_code == 200
    assert client.delete("/internal/admin/billing/rates/video_generation/seedance-2.5/*").status_code == 404


def test_charges_summary_and_csv_export(store, monkeypatch) -> None:
    created = store.create("视频", None, None)
    store.hold(created["id"], "req-a", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=8_870_970, source="measured"))
    store.settle("req-a", "succeeded")

    client = _client(store, monkeypatch)
    listing = client.get("/internal/admin/billing/charges", params={"from": "2020-01-01", "to": "2099-01-01"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    summary = client.get("/internal/admin/billing/summary", params={"from": "2020-01-01", "to": "2099-01-01"})
    assert summary.json()["amount_yuan"] == 8.871

    exported = client.get("/internal/admin/billing/export.csv")
    assert exported.status_code == 200
    text = exported.content.decode("utf-8-sig")
    assert "北京时间" in text
    assert "seedance-2.5" in text
    assert "8.870970" in text


def test_summary_ignores_refunded_charges(store, monkeypatch) -> None:
    created = store.create("视频", None, None)
    store.hold(created["id"], "req-ok", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=8_870_970, source="measured"))
    store.settle("req-ok", "succeeded")
    store.hold(created["id"], "req-bad", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=8_870_970, source="measured"))
    store.settle("req-bad", "failed")

    client = _client(store, monkeypatch)
    summary = client.get("/internal/admin/billing/summary", params={"from": "2020-01-01", "to": "2099-01-01"}).json()
    # 失败的那单钱退回去了，不能算进「花了多少」；明细里仍然有两行。
    assert summary["amount_yuan"] == 8.871
    assert summary["count"] == 1
    assert client.get("/internal/admin/billing/charges").json()["total"] == 2


def test_since_filters_out_old_charges(store, monkeypatch) -> None:
    created = store.create("视频", None, None)
    store.hold(created["id"], "req-a", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=1_000_000, source="measured"))
    client = _client(store, monkeypatch)
    future = client.get("/internal/admin/billing/charges", params={"from": "2099-01-01"})
    assert future.json()["total"] == 0


def test_terminal_job_status_settles_without_any_poll(store, monkeypatch) -> None:
    """不轮询也要结账：worker 自己把作业判成终态时，held 行必须跟着落定。

    原先只在查询口结算，没人轮询的作业（或 worker 超时判死的）会一直挂着 held，
    对「累计总额」窗口就是永久占位——那笔钱再也退不回来。
    """
    from control_plane import ai_capabilities
    from control_plane.ai_capabilities import CapabilityStore

    monkeypatch.setattr(ai_capabilities, "_api_key_store", lambda: store)
    # 用 mkdtemp 不清理：Windows 上 sqlite 句柄没关，rmtree 会 PermissionError。
    caps = CapabilityStore(Path(tempfile.mkdtemp()) / "cap.db", "unit-secret")
    job_id = caps.create_job("seedance-2.5", "jmapi", {"prompt": "x"})
    created = store.create("视频", None, None)
    held = store.hold(created["id"], "req-job", "video_generation", "seedance-2.5", "jmapi",
                      Quote(unit="second", quantity=5, amount_micro=8_870_970, source="measured"))
    store.link(str(held["charge_id"]), job_id)

    caps.update_job(job_id, stage="polling")
    assert store.charges()["items"][0]["state"] == "held"

    caps.update_job(job_id, status="failed")
    row = store.charges()["items"][0]
    assert row["state"] == "refunded"
    assert row["job_status"] == "failed"
