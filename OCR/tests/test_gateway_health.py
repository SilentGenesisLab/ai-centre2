from ocr_service.gateway import _pool_health


def test_pool_health_is_available_with_one_healthy_worker() -> None:
    assert _pool_health(1, 2) == ("ok", "degraded")


def test_pool_health_requires_at_least_one_worker() -> None:
    assert _pool_health(0, 2) == ("degraded", "unavailable")


def test_pool_health_reports_full_capacity() -> None:
    assert _pool_health(2, 2) == ("ok", "full")
