from __future__ import annotations

import uvicorn

from .config import load_config


def main() -> None:
    config = load_config()
    uvicorn.run(
        "ocr_service.api:app",
        host=config.host,
        port=config.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
