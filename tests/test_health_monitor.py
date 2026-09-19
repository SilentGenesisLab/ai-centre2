from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from control_plane.health_monitor import HealthMonitorStore, _percentile, safe_alert_error


def test_nearest_rank_and_empty_samples() -> None:
    assert _percentile([], .95) is None
    assert _percentile([1, 2, 3, 4], .5) == 2
    assert _percentile([1, 2, 3, 4], .95) == 4


def test_two_failures_and_two_successes_drive_incident_state() -> None:
    with TemporaryDirectory() as directory:
        store = HealthMonitorStore(Path(directory) / "health.db", "test-secret")
        assert store.record("asr", "l1", False, 10, error="timeout") is None
        transition = store.record("asr", "l1", False, 11, error="timeout")
        assert transition and transition["kind"] == "failure"
        assert store.record("asr", "l1", True, 8) is None
        transition = store.record("asr", "l1", True, 7)
        assert transition and transition["kind"] == "recovery"
        item = next(x for x in store.status()["items"] if x["id"] == "asr")
        assert item["current_status"] == "operational"
        assert item["availability"] == 50.0


def test_webhook_is_encrypted_and_never_returned() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "health.db"
        store = HealthMonitorStore(path, "test-secret")
        public = store.save_config(True, "https://open.feishu.cn/open-apis/bot/v2/hook/abc", "secret")
        assert public["configured"] is True
        assert public["webhook_host"] == "open.feishu.cn"
        assert "webhook_url" not in public and "secret" not in public
        assert b"open-apis/bot" not in path.read_bytes()


def test_skipped_check_does_not_change_availability() -> None:
    with TemporaryDirectory() as directory:
        store = HealthMonitorStore(Path(directory) / "health.db", "test-secret")
        store.record("tts", "l2", None, None, skipped_reason="busy")
        item = next(x for x in store.status()["items"] if x["id"] == "tts")
        assert item["availability"] is None
        assert item["sample_count"] == 0


def test_errors_are_sanitized() -> None:
    assert "https://" not in safe_alert_error("failed https://x.test/a?token=abc")
