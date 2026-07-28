from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .service import TTSService


@lru_cache(maxsize=1)
def get_tts_service() -> TTSService:
    return TTSService(get_settings())

