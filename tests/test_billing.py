from pathlib import Path
import tempfile
from tempfile import TemporaryDirectory

import pytest

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from control_plane import api
from control_plane.api_keys import ApiKeyStore
from control_plane.billing_api import public_router
from control_plane.billing_api import router as billing_router
from control_plane.credit_pricing import Quote


def _client(store: ApiKeyStore, monkeypatch) -> TestClient:
    monkeypatch.setattr(api, "get_api_key_store", lambda: store)
    app = FastAPI()
    # 鉴权在 api.py 里由 include_router 统一挂，这里只测处理逻辑。
    app.include_router(billing_router)
    app.include_router(public_router)
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


def test_deleted_key_spend_is_not_labelled_as_service_token(store, monkeypatch) -> None:
    created = store.create("临时", None, None)
    store.hold(created["id"], "req-del", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=1_000_000, source="measured"))
    store.settle("req-del", "succeeded")
    store.delete(created["id"])

    by_key = _client(store, monkeypatch).get(
        "/internal/admin/billing/summary", params={"from": "2020-01-01", "to": "2099-01-01"}
    ).json()["by_key"]
    assert [row["key"] for row in by_key] == ["(已删除的 Key)"]


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
    job_id, _ = caps.create_job("seedance-2.5", "jmapi", {"prompt": "x"})
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


class _AsKey:
    """冒充 require_service_token：在请求上下文里把当前 Key 设好。"""

    def __init__(self, key_id: str) -> None:
        self.key_id = key_id

    async def __call__(self) -> None:
        from control_plane.generation_jobs import current_api_key_id

        current_api_key_id.set(self.key_id)


class _Catalog:
    def models(self):
        return [
            {
                "code": "seedance-2.5",
                "channels": [
                    {"channel_code": "libtv", "channel_name": "LibTV", "enabled": True,
                     "channel_enabled": True, "health_status": "online"},
                    {"channel_code": "jmapi", "channel_name": "即梦", "enabled": True,
                     "channel_enabled": False, "health_status": "offline"},
                ],
            }
        ]


def test_pricing_lists_every_model_with_credits_and_channels(store, monkeypatch) -> None:
    monkeypatch.setattr(api, "get_capability_store", lambda: _Catalog())
    client = _client(store, monkeypatch)

    body = client.get("/v1/pricing/models").json()
    assert body["credits_per_yuan"] == 62
    row = next(item for item in body["items"]
               if item["model"] == "seedance-2.5" and item["variant"] == "*")
    # 110 积分/秒 ÷ 62 = ¥1.774194，反过来乘回 62 要还原成 110 积分。
    assert row["amount_micro"] == 1_774_194
    assert row["credits"] == 110.0
    assert row["unit_name"] == "秒"
    assert [bind["code"] for bind in row["channels"]] == ["libtv", "jmapi"]
    # 只有一个渠道是启用的，模型整体仍算可用。
    assert row["available"] is True

    filtered = client.get("/v1/pricing/models", params={"service": "video_generation"}).json()["items"]
    assert filtered and all(item["service"] == "video_generation" for item in filtered)
    assert len(filtered) < body["total"]


def test_usage_reports_credits_and_calls_per_model(store, monkeypatch) -> None:
    monkeypatch.setattr(api, "get_capability_store", lambda: _Catalog())
    created = store.create("视频", None, None)
    store.hold(created["id"], "req-1", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=8_870_970, source="measured"))
    store.settle("req-1", "succeeded")
    store.hold(created["id"], "req-2", "image_generation", "gpt-image-2.5", "grsai",
               Quote(unit="image", quantity=1, amount_micro=300_000, source="estimated"))
    store.settle("req-2", "succeeded")
    store.hold(created["id"], "req-3", "image_generation", "gpt-image-2.5", "grsai",
               Quote(unit="image", quantity=1, amount_micro=300_000, source="estimated"))
    store.settle("req-3", "failed")

    client = _client(store, monkeypatch)
    body = client.get("/v1/usage", params={"window": "total"}).json()

    assert body["count"] == 2, "退回的失败单不算消耗，也不能算一次调用"
    assert body["amount_yuan"] == 9.171
    assert body["credits"] == round(9_170_970 / 1_000_000 * 62, 4)
    by_model = {row["model"]: row for row in body["by_model"]}
    assert by_model["seedance-2.5"]["count"] == 1
    assert by_model["gpt-image-2.5"]["count"] == 1
    assert set(body["windows"]) == {"5h", "24h", "total"}
    assert body["windows"]["total"]["amount_micro"] == 9_170_970


def test_usage_labels_model_less_services_by_service_name(store, monkeypatch) -> None:
    """超分/切分这类没有型号的服务，用量页按服务名归类，不要出现「(未知)」。"""
    created = store.create("视频", None, None)
    store.hold(created["id"], "req-up", "upscale", "", "local",
               Quote(unit="call", quantity=1, amount_micro=500_000, source="estimated"))
    store.settle("req-up", "succeeded")

    client = _client(store, monkeypatch)
    rows = client.get("/v1/usage", params={"window": "total"}).json()["by_model"]
    assert [(row["service"], row["model"]) for row in rows] == [("upscale", "upscale")]


def test_usage_is_scoped_to_the_calling_key(store, monkeypatch) -> None:
    first = store.create("甲", None, None)
    second = store.create("乙", None, None)
    store.hold(first["id"], "req-a", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=1, amount_micro=1_000_000, source="measured"))
    store.settle("req-a", "succeeded")
    store.hold(second["id"], "req-b", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=2, amount_micro=2_000_000, source="measured"))
    store.settle("req-b", "succeeded")

    client = _client(store, monkeypatch)
    assert client.get("/v1/usage").json()["amount_micro"] == 3_000_000

    # 客户 Key 的视角：鉴权依赖在请求上下文里 set 这个 ContextVar，外面的线程里 set 传不进去。
    scoped_app = FastAPI()
    scoped_app.include_router(public_router, dependencies=[Depends(_AsKey(first["id"]))])
    scoped = TestClient(scoped_app).get("/v1/usage").json()
    assert scoped["scope"] == "api_key"
    assert scoped["amount_micro"] == 1_000_000
    assert scoped["count"] == 1


class _WildcardCatalog:
    def models(self):
        return [
            {"code": "kling-4.0",
             "channels": [{"channel_code": "libtv", "channel_name": "LibTV", "enabled": True,
                           "channel_enabled": True, "health_status": "online"}]},
            {"code": "kling-4.0-flash",
             "channels": [{"channel_code": "libtv", "channel_name": "LibTV", "enabled": True,
                           "channel_enabled": True, "health_status": "online"}]},
        ]


def test_wildcard_rate_rows_resolve_to_real_model_channels(store, monkeypatch) -> None:
    """`kling-*` 是通配费率行，之前拿它去查渠道表永远查不到 → 可用型号被标成 available=false。"""
    monkeypatch.setattr(api, "get_capability_store", lambda: _WildcardCatalog())
    client = _client(store, monkeypatch)

    rows = client.get("/v1/pricing/models", params={"model": "kling-4.0"}).json()["items"]
    assert [row["channels"] for row in rows] and all(
        [bind["code"] for bind in row["channels"]] == ["libtv"] for row in rows
    )
    assert all(row["available"] is True for row in rows)


def test_promo_rates_zero_out_only_the_named_models_until_the_deadline(store, monkeypatch) -> None:
    from control_plane.credit_pricing import estimate_request

    client = _client(store, monkeypatch)
    store.set_settings({
        "promo_free_items": [{"service": "video_generation", "model": "kling-4.0-flash", "unit": "second"}],
        "promo_free_until": "2099-01-01T00:00:00+08:00",
    })

    rates = store.rates_for_billing()
    discounted = estimate_request(rates, "video_generation", {"resolution": "720p"}, "kling-4.0-flash")
    normal = estimate_request(rates, "video_generation", {"resolution": "720p"}, "kling-4.0")

    assert discounted.amount_micro == 0
    assert discounted.source == "promo"
    assert normal.amount_micro > 0
    # 原价行没被改写：促销只多一行 0 元，过期自动失效。
    assert any(row["model"] == "kling-4.0-flash" and row["amount_micro"] > 0 for row in store.rates_cached())
    assert client.get("/internal/admin/billing/settings").json()["promo_active"] is True

    store.set_settings({"promo_free_until": "2000-01-01T00:00:00+08:00"})
    assert store.promo_active() is False
    assert estimate_request(store.rates_for_billing(), "video_generation", {}, "kling-4.0-flash").amount_micro > 0


def test_pricing_table_shows_the_promo_price_and_the_original(store, monkeypatch) -> None:
    """客户看的是这张表：活动期挂牌价必须并行显示 0，否则会以为没生效。"""
    monkeypatch.setattr(api, "get_capability_store", lambda: _WildcardCatalog())
    client = _client(store, monkeypatch)
    store.set_settings({
        "promo_free_items": [
            {"service": "video_generation", "model": "kling-4.0-flash", "unit": "second"},
            {"service": "video_generation", "model": "minimax-h3-hs", "unit": "second"},
        ],
        "promo_free_until": "2099-01-01T00:00:00+08:00",
    })

    body = client.get("/v1/pricing/models", params={"service": "video_generation"}).json()
    assert body["promo_active"] is True
    rows = {item["model"]: item for item in body["items"]}

    # 费率表里有独立行的型号：原行改挂 0 元，原价留在 original_* 里。
    flash = rows["kling-4.0-flash"]
    assert (flash["amount_micro"], flash["source"]) == (0, "promo")
    assert flash["original_amount_yuan"] == 0.23
    assert rows["kling-4.0"]["amount_micro"] > 0, "没进促销的型号不能被带着一起降价"

    # 只靠 `kling-*` 通配定价、没有独立行的型号：补一行 0 元，原价按通配算。
    hs = rows["minimax-h3-hs"]
    assert (hs["amount_micro"], hs["source"]) == (0, "promo")
    assert hs["original_amount_yuan"] == 0.23


def test_promo_settings_are_validated_and_round_trip(store, monkeypatch) -> None:
    client = _client(store, monkeypatch)
    response = client.put("/internal/admin/billing/settings", json={
        "promo_free_items": [{"service": "video_generation", "model": "minimax-h3", "unit": "second"}],
        "promo_free_until": "2026-11-11T23:59:59+08:00",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["promo_free_items"] == [
        {"service": "video_generation", "model": "minimax-h3", "unit": "second"}
    ]
    assert body["promo_free_until"] == "2026-11-11T23:59:59+08:00"

    assert client.put("/internal/admin/billing/settings",
                      json={"promo_free_until": "not-a-date"}).status_code == 400


def test_reconcile_settles_stuck_held_rows_and_leaves_live_jobs_alone(store, monkeypatch) -> None:
    """H3 只靠客户端轮询结算：没人轮询，作业成功了账上还是 held，额度被永久占住。"""
    from control_plane import billing_reconcile

    monkeypatch.setattr(api, "get_api_key_store", lambda: store)
    created = store.create("视频", None, None)
    for request_id, quote_micro in (("req-done", 1_000_000), ("req-live", 1_000_000),
                                     ("req-orphan", 1_000_000)):
        held = store.hold(created["id"], request_id, "video_generation", "seedance-2.5", "jmapi",
                          Quote(unit="second", quantity=1, amount_micro=quote_micro, source="measured"))
        if request_id != "req-orphan":
            store.link(str(held["charge_id"]), f"job-{request_id}")
    # 把三条都倒回 7 小时前，跨过清扫阈值。
    with store._connect() as db:
        db.execute("UPDATE usage_charges SET created_at='2020-01-01T00:00:00.000+00:00'")

    statuses = {"job-req-done": "succeeded", "job-req-live": "running"}
    result = billing_reconcile.reconcile_held(status_of=statuses.get)

    assert result == {"settled": 1, "refunded": 1, "pending": 1}
    states = {row["request_id"]: row["state"] for row in store.charges()["items"]}
    assert states["req-done"] == "spent"
    assert states["req-orphan"] == "refunded"
    assert states["req-live"] == "held", "还在跑的单不能退，宁可继续占着额度"


def test_balance_reports_limits_and_remaining(store, monkeypatch) -> None:
    created = store.create("先钦2", 20000, None,
                           limit_5h_micro=5_000_000_000,
                           limit_24h_micro=8_000_000_000,
                           limit_total_micro=20_000_000_000)
    store.hold(created["id"], "req-bal", "video_generation", "seedance-2.5", "jmapi",
               Quote(unit="second", quantity=5, amount_micro=8_870_970, source="measured"))
    store.settle("req-bal", "succeeded")

    app = FastAPI()
    monkeypatch.setattr(api, "get_api_key_store", lambda: store)
    app.include_router(public_router, dependencies=[Depends(_AsKey(created["id"]))])
    body = TestClient(app).get("/v1/balance").json()

    assert body["scope"] == "api_key"
    assert body["key_name"] == "先钦2"
    assert body["request_quota"] == {"quota": 20000, "used": 0, "remaining": 20000}
    five_hour = body["windows"]["5h"]
    assert five_hour["amount_micro"] == 8_870_970
    assert five_hour["limit_yuan"] == 5000.0
    assert five_hour["remaining_micro"] == 5_000_000_000 - 8_870_970
    assert body["windows"]["24h"]["remaining_yuan"] == 7991.1290
    assert "预付费钱包" in body["top_up"]
