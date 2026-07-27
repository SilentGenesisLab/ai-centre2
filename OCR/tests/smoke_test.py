from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from ocr_service.api import app


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        image_path = Path(tmp) / "sample.png"
        image = Image.new("RGB", (640, 240), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 80), "Las arrugas son pequenos problemas", fill="black")
        image.save(image_path)

        client = TestClient(app)
        health = client.get("/health")
        print("health", health.status_code, health.json())

        payload = {
            "job_id": "smoke",
            "source_lang_hint": "es",
            "images": [
                {
                    "image_id": "sample",
                    "path": str(image_path),
                    "time": 0.0,
                    "regions": [{"name": "full", "bbox": [0, 0, 640, 240]}],
                }
            ],
        }
        response = client.post("/v1/ocr/batch", json=payload)
        print("ocr", response.status_code)
        print(json.dumps(response.json(), ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
