from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from control_plane.ai_capabilities import CapabilityStore
from control_plane.config import get_settings
from control_plane.generation_tasks import _headers, _payload


def redact(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(word in lowered for word in ("token", "secret", "credential", "authorization")):
        return "***"
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if isinstance(value, dict):
        return {name: redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("channel", choices=("jmapi", "libtv"))
    args = parser.parse_args()
    settings = get_settings()
    store = CapabilityStore(settings.ai_capabilities_db_path, settings.service_token)
    binding = store.binding("seedance-2.0", args.channel)
    request = {
        "prompt": "A blue glass bottle in a clean studio, slow camera push-in",
        "reference_image_urls": [],
        "reference_video_urls": [],
        "reference_audio_urls": [],
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "sound": False,
    }
    payload = _payload(binding, request, settings.video_generation_blank_image_url)
    response = httpx.post(
        urljoin(
            binding["base_url"].rstrip("/") + "/",
            binding["submit_path"].lstrip("/"),
        ),
        headers=_headers(binding),
        json=payload,
        timeout=httpx.Timeout(binding["timeout_seconds"], connect=20),
        follow_redirects=False,
    )
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:2000]
    print(
        json.dumps(
            {
                "channel": args.channel,
                "status_code": response.status_code,
                "payload": redact(payload),
                "response": redact(body),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
