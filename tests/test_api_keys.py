from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from control_plane.api_keys import ApiKeyStore, CreditQuotaExceeded
from control_plane.credit_pricing import Quote, estimate_request, from_yuan


def quote(amount_micro: int, unit: str = "call", quantity: float = 1.0) -> Quote:
    return Quote(unit=unit, quantity=quantity, amount_micro=amount_micro, source="manual")


def test_api_key_edit_preserves_identity_and_usage() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("原名称", 100, "2099-01-01T00:00:00.000+00:00")
        ok, _, _ = store.authenticate_and_consume(created["api_key"])
        assert ok
        updated = store.update(created["id"], name="  新名称  ", quota=200, expires_at=None)
        assert updated is not None
        assert updated["id"] == created["id"]
        assert updated["prefix"] == created["prefix"]
        assert updated["name"] == "新名称"
        assert updated["quota"] == 200
        assert updated["used"] == 1
        assert updated["expires_at"] is None


def test_hold_blocks_before_any_row_is_written() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, limit_24h_micro=1000)
        store.hold(created["id"], "req-a", "video_generation", "seedance-2.5", "jmapi", quote(600))
        with pytest.raises(CreditQuotaExceeded) as caught:
            store.hold(created["id"], "req-b", "video_generation", "seedance-2.5", "jmapi", quote(600))
        assert caught.value.reason == "key_24h"
        assert store.charges()["total"] == 1


def test_windows_are_independent() -> None:
    """只超 5 小时那档时，24 小时与总额仍该放行。"""
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, limit_5h_micro=1000, limit_24h_micro=10_000)
        store.hold(created["id"], "req-a", "video_generation", "x", "jmapi", quote(600))
        with pytest.raises(CreditQuotaExceeded) as caught:
            store.hold(created["id"], "req-b", "video_generation", "x", "jmapi", quote(600))
        assert caught.value.reason == "key_5h"


def test_failed_job_refunds_and_resettle_is_noop() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, limit_24h_micro=1000)
        store.hold(created["id"], "req-a", "video_generation", "x", "jmapi", quote(600))
        store.settle("req-a", "failed")
        assert store.usage(created["id"])["spent"]["24h"] == 0
        store.hold(created["id"], "req-b", "video_generation", "x", "jmapi", quote(900))
        assert store.usage(created["id"])["spent"]["24h"] == 900
        store.settle("req-b", "succeeded")
        store.settle("req-b", "failed")
        assert store.usage(created["id"])["spent"]["24h"] == 900


def test_platform_gate_blocks_every_key() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        store.set_settings({"platform_limit_24h_micro": 1000})
        first = store.create("甲", None, None)
        second = store.create("乙", None, None)
        store.hold(first["id"], "req-a", "video_generation", "x", "jmapi", quote(600))
        with pytest.raises(CreditQuotaExceeded) as caught:
            store.hold(second["id"], "req-b", "video_generation", "x", "jmapi", quote(600))
        assert caught.value.reason == "platform_24h"
        assert "platform 24h" in str(caught.value)


def test_unlimited_key_is_not_limited_by_money() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("老 Key", None, None)
        store.hold(created["id"], "req-a", "video_generation", "x", "jmapi", quote(100_000_000))
        assert store.usage(created["id"])["spent"]["total"] == 100_000_000


def test_estimate_uses_most_specific_rate() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        rates = store.rates()
        seedance = estimate_request(rates, "video_generation", {"duration_seconds": 5}, "seedance-2.5")
        assert seedance.amount_micro == 8_870_970
        assert seedance.unit == "second"
        assert seedance.source == "measured"
        free = estimate_request(rates, "image_generation", {"image_size": "2K"}, "moss-image")
        assert (free.amount_micro, free.source) == (0, "free")
        unknown = estimate_request(rates, "nonexistent_service", {}, "x")
        assert (unknown.amount_micro, unknown.source) == (0, "unknown")


def test_yuan_micro_round_trip() -> None:
    assert from_yuan("1.774194") == 1_774_194
    assert from_yuan(None) is None
    assert from_yuan("") is None
    assert from_yuan(0) == 0
    with pytest.raises(ValueError):
        from_yuan("-1")


def test_charges_filters_and_summary() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None)
        store.hold(created["id"], "req-a", "video_generation", "seedance-2.5", "jmapi",
                   quote(2_000_000, "second", 2))
        store.settle("req-a", "succeeded")
        store.hold(created["id"], "req-b", "image_generation", "gpt-image-2.5", "grsai",
                   quote(300_000, "image", 1))
        store.settle("req-b", "failed")

        assert store.charges()["total"] == 2
        assert store.charges(service="video_generation")["total"] == 1
        assert store.charges(model="gpt-image-2.5")["total"] == 1
        assert store.charges(channel="grsai")["total"] == 1
        assert store.charges(state="spent")["total"] == 1
        assert store.charges(keyword="视频")["total"] == 2

        summary = store.summary()
        # 失败那单（req-b）已经退回，不算消耗——和窗口合计同一口径。
        assert summary["amount_micro"] == 2_000_000
        assert summary["amount_yuan"] == 2.0
        assert summary["count"] == 1
        assert summary["platform"]["spent"]["24h"] == 2_000_000
        assert summary["by_model"][0]["model"] == "seedance-2.5"
