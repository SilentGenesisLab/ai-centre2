from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from control_plane.api_keys import ApiKeyStore, CreditQuotaExceeded
from control_plane.credit_pricing import estimate


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


def test_credit_quota_blocks_before_spending() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, daily_credit_quota=1000)
        store.charge(created["id"], "job-a", 600)
        assert store.usage(created["id"]) == 600
        with pytest.raises(CreditQuotaExceeded):
            store.charge(created["id"], "job-b", 600)
        assert store.usage(created["id"]) == 600
        assert store.list()[0]["credit_used_today"] == 600


def test_failed_job_refunds_credits() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, daily_credit_quota=1000)
        store.charge(created["id"], "job-a", 600)
        store.settle("job-a", "failed")
        assert store.usage(created["id"]) == 0
        store.charge(created["id"], "job-b", 900)
        assert store.usage(created["id"]) == 900


def test_succeeded_job_keeps_charge_and_resettle_is_noop() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, daily_credit_quota=1000)
        store.charge(created["id"], "job-a", 400)
        store.settle("job-a", "succeeded")
        store.settle("job-a", "failed")
        assert store.usage(created["id"]) == 400


def test_unchanged_keys_are_not_limited_by_credits() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("老 Key", None, None)
        store.charge(created["id"], "job-a", 100000)
        assert store.usage(created["id"]) == 100000


def test_estimate_uses_model_rate_and_duration() -> None:
    assert estimate({"model": "seedance-2.5", "duration_seconds": 5}) == 550
    assert estimate({"model": "seedance-2.0-fast", "duration_seconds": 5}) == 110
    assert estimate({"model": "gpt-image-2.5"}) == 0
