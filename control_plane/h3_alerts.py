from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from typing import Any

import httpx

from .config import Settings


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def safe_alert_error(value: str | None) -> str:
    text = _URL_RE.sub("[URL已隐藏]", str(value or "未知错误"))
    text = re.sub(r"(?i)(token|secret|key)\s*[:=]\s*\S+", r"\1=[已隐藏]", text)
    return text[:300]


class H3LarkWebhook:
    def __init__(self, settings: Settings) -> None:
        webhook = getattr(settings, "h3_lark_webhook_url", None)
        secret = getattr(settings, "h3_lark_webhook_secret", None)
        self.webhook_url = webhook.get_secret_value() if webhook else ""
        self.secret = secret.get_secret_value() if secret else ""
        self.timeout = float(getattr(settings, "h3_lark_timeout_seconds", 5.0))

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def _signed_payload(
        self, payload: dict[str, Any], timestamp: int | None = None
    ) -> dict[str, Any]:
        if self.secret:
            current = int(timestamp or time.time())
            string_to_sign = f"{current}\n{self.secret}"
            digest = hmac.new(
                string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
            ).digest()
            payload.update(
                timestamp=str(current), sign=base64.b64encode(digest).decode("ascii")
            )
        return payload

    def _payload(self, text: str, timestamp: int | None = None) -> dict[str, Any]:
        return self._signed_payload(
            {"msg_type": "text", "content": {"text": text}}, timestamp
        )

    def _card_payload(
        self,
        *,
        title: str,
        template: str,
        fields: list[tuple[str, str]],
        detail: str | None = None,
        button_url: str | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        elements: list[dict[str, Any]] = [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{label}**\n{value}",
                        },
                    }
                    for label, value in fields
                ],
            }
        ]
        if detail:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**错误摘要**\n{safe_alert_error(detail)}",
                    },
                }
            )
        if button_url:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看后台详情"},
                            "type": "primary",
                            "url": button_url,
                        }
                    ],
                }
            )
        return self._signed_payload(
            {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "template": template,
                        "title": {"tag": "plain_text", "content": title},
                    },
                    "elements": elements,
                },
            },
            timestamp,
        )

    def _post(self, payload: dict[str, Any]) -> None:
        if not self.configured:
            raise RuntimeError("飞书群机器人Webhook未配置")
        response = httpx.post(
            self.webhook_url, json=payload, timeout=self.timeout, trust_env=False
        )
        response.raise_for_status()
        result = response.json()
        code = result.get("code", result.get("StatusCode", 0))
        if code not in {0, "0", None}:
            raise RuntimeError("飞书群机器人拒绝了通知")

    def send(self, text: str) -> None:
        self._post(self._payload(text))

    def send_card(
        self,
        *,
        title: str,
        template: str,
        fields: list[tuple[str, str]],
        detail: str | None = None,
        button_url: str | None = None,
    ) -> None:
        self._post(
            self._card_payload(
                title=title,
                template=template,
                fields=fields,
                detail=detail,
                button_url=button_url,
            )
        )
