from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

from control_plane.h3_alerts import H3LarkWebhook, safe_alert_error


def test_lark_signature_and_secret_redaction() -> None:
    notifier = H3LarkWebhook(SimpleNamespace(
        h3_lark_webhook_url=SecretStr("https://example.com/hook"),
        h3_lark_webhook_secret=SecretStr("secret"),
        h3_lark_timeout_seconds=5,
    ))
    payload = notifier._payload("hello", timestamp=1700000000)
    assert payload["timestamp"] == "1700000000"
    assert payload["sign"]
    assert "secret" not in str(payload)


def test_alert_error_hides_urls_and_credentials() -> None:
    value = safe_alert_error("failed https://example.com/hook token=abcdef")
    assert "example.com" not in value
    assert "abcdef" not in value
