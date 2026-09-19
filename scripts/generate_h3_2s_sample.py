from __future__ import annotations

import json
import time

import httpx

from control_plane.config import get_settings


def main() -> int:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.service_token}"}
    with httpx.Client(
        base_url="http://127.0.0.1:8320",
        headers=headers,
        timeout=60,
    ) as client:
        capacity = client.get(
            "/v1/video-generations/minimax-h3/workers/status"
        )
        capacity.raise_for_status()
        print(json.dumps({"workers": capacity.json()}, ensure_ascii=False), flush=True)

        response = client.post(
            "/v1/video-generations/minimax-h3/jobs",
            json={
                "prompt": (
                    "一颗透明蓝色玻璃球悬浮在淡蓝色科技展台上，"
                    "镜头缓慢推进，柔和轮廓光，干净背景，商业广告质感"
                ),
                "duration_seconds": 2,
                "resolution": "480p",
                "quality": "low",
                "aspect_ratio": "16:9",
                "priority": 800,
                "seed": 20260910,
                "external_ref": "h3-2s-preview-20260910",
                "metadata": {"purpose": "two-second-range-validation"},
            },
        )
        response.raise_for_status()
        submitted = response.json()
        job_id = str(submitted["job_id"])
        print(json.dumps(submitted, ensure_ascii=False), flush=True)

        deadline = time.monotonic() + 3600
        previous = None
        while time.monotonic() < deadline:
            response = client.get(
                f"/v1/video-generations/minimax-h3/jobs/{job_id}"
            )
            response.raise_for_status()
            status = response.json()
            signature = (
                status.get("status"),
                status.get("stage"),
                status.get("progress"),
            )
            if signature != previous:
                print(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "status": signature[0],
                            "stage": signature[1],
                            "progress": signature[2],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                previous = signature
            if status.get("status") in {"succeeded", "failed", "cancelled"}:
                print(json.dumps(status, ensure_ascii=False), flush=True)
                return 0 if status.get("status") == "succeeded" else 1
            time.sleep(5)
    raise TimeoutError(f"MiniMax H3 job {job_id} did not finish within one hour")


if __name__ == "__main__":
    raise SystemExit(main())
