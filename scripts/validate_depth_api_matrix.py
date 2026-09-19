from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from control_plane.config import get_settings
from control_plane.depth_tasks import _upload_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the four depth variants through the API.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8320")
    parser.add_argument(
        "--variants",
        nargs="+",
        default=["da2:small", "da2:base", "da3:small", "da3:base"],
    )
    return parser.parse_args()


def without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def main() -> None:
    args = parse_args()
    settings = get_settings()
    source_url = _upload_result(
        args.input,
        {"filename": args.input.name, "external_ref": "depth-matrix-validation-20260820"},
        "depth-matrix-validation-20260820",
    )
    headers = {"Authorization": f"Bearer {settings.service_token}"}
    results: list[dict[str, object]] = []
    with httpx.Client(timeout=httpx.Timeout(settings.depth_wait_timeout_seconds, connect=30)) as client:
        for raw_variant in args.variants:
            version, model = raw_variant.split(":", 1)
            if version not in {"da2", "da3"} or model not in {"small", "base"}:
                raise SystemExit(f"invalid variant: {raw_variant}")
            started = time.perf_counter()
            response = client.post(
                f"{args.base_url}/v1/video-depth/jobs/wait",
                headers=headers,
                json={
                    "source_uri": source_url,
                    "version": version,
                    "model": model,
                    "filename": f"input30-{version}-{model}.mp4",
                    "input_size": 518,
                    "max_resolution": 960,
                    "target_fps": -1,
                    "external_ref": f"depth-matrix-{version}-{model}-20260820",
                },
            )
            response.raise_for_status()
            payload = response.json()
            artifact = client.get(payload["video_url"], headers={"Range": "bytes=0-1023"})
            if artifact.status_code not in {200, 206} or not artifact.content:
                raise RuntimeError(f"unreadable output for {version}/{model}")
            payload["api_elapsed_seconds"] = round(time.perf_counter() - started, 3)
            payload["artifact_http_status"] = artifact.status_code
            results.append(payload)
            print(
                json.dumps(
                    {
                        "version": version,
                        "model": model,
                        "job_id": payload["job_id"],
                        "status": payload["status"],
                        "video_url": without_query(payload["video_url"]),
                        "effective_input_size": payload["effective_input_size"],
                        "peak_vram_gb": payload["peak_vram_gb"],
                        "output_width": payload["output_width"],
                        "output_height": payload["output_height"],
                        "api_elapsed_seconds": payload["api_elapsed_seconds"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    report = {
        "input_name": args.input.name,
        "source_url": without_query(source_url),
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
