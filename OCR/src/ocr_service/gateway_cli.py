from __future__ import annotations

import uvicorn

from .config import load_gateway_config


def main() -> None:
    config = load_gateway_config()
    uvicorn.run(
        "ocr_service.gateway:app",
        host=config.host,
        port=config.port,
        log_level="info",
        reload=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
