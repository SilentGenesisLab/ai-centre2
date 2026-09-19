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


def test_lark_interactive_card_payload() -> None:
    notifier = H3LarkWebhook(SimpleNamespace(
        h3_lark_webhook_url=SecretStr("https://example.com/hook"),
        h3_lark_webhook_secret=None,
        h3_lark_timeout_seconds=5,
    ))
    payload = notifier._card_payload(
        title="🚨 AI Centre 故障告警",
        template="red",
        fields=[("服务", "OCR文字识别"), ("当前状态", "连续2次鉴活失败")],
        detail="ConnectError: failed https://private.example/path token=secret",
        button_url="https://aicentre2.sligenai.cn:8443/admin/resources",
    )
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["header"]["template"] == "red"
    assert payload["card"]["elements"][0]["fields"][0]["text"]["content"] == "**服务**\nOCR文字识别"
    assert payload["card"]["elements"][-1]["actions"][0]["url"].endswith("/admin/resources")
    assert "private.example" not in str(payload)
    assert "token=secret" not in str(payload)
