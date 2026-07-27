from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "control_plane.api:app",
        host=settings.control_host,
        port=settings.control_port,
        workers=1,
    )


if __name__ == "__main__":
    main()

