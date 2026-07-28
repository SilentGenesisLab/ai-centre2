from __future__ import annotations

import httpx

from ..base import PermanentTTSProviderError, TransientTTSProviderError


def raise_for_provider_status(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    message = response.text[:500].strip()
    detail = f"{provider} returned HTTP {response.status_code}"
    if message:
        detail = f"{detail}: {message}"
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
