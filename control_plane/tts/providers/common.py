from __future__ import annotations

import httpx

from ..base import PermanentTTSProviderError, TransientTTSProviderError


def raise_for_provider_status(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    detail = f"{provider} returned HTTP {response.status_code}"
    request_id = (
        response.headers.get("x-request-id")
        or response.headers.get("request-id")
        or response.headers.get("x-tt-logid")
    )
    if request_id:
        detail = f"{detail} (request_id={request_id[:128]})"
    # Never echo a 5xx response body. Model gateways commonly include local
    # paths and Python/CUDA tracebacks in it. For client errors, expose only a
    # short provider-authored message field instead of the complete payload.
    if response.status_code < 500:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str) and message.strip():
                safe_message = " ".join(message.split())[:160]
                detail = f"{detail}: {safe_message}"
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientTTSProviderError(detail)
    raise PermanentTTSProviderError(detail)


def translate_network_error(exc: Exception, provider: str) -> Exception:
    if isinstance(exc, httpx.RequestError):
        return TransientTTSProviderError(
            f"{provider} network failure: {type(exc).__name__}"
        )
    return PermanentTTSProviderError(
        f"{provider} request failed: {type(exc).__name__}"
    )
