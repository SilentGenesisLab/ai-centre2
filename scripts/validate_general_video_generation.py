from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from control_plane.config import get_settings


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class Case:
    name: str
    channel: str
    with_video: bool


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate both general video generation channels without printing secrets."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8320")
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument(
        "--resolution",
        choices=("480p", "720p", "1080p", "2K"),
        default="720p",
    )
    parser.add_argument(
        "--reference-video",
        default=None,
        help="Public HTTPS reference video; defaults to HEALTH_MONITOR_PROBE_VIDEO_URL.",
    )
    parser.add_argument(
        "--cases",
        default="jmapi-text,libtv-text,jmapi-video,libtv-video",
        help="Comma-separated case names.",
    )
    args = parser.parse_args()

    settings = get_settings()
    reference_video = args.reference_video or settings.health_monitor_probe_video_url
    known = {
        "jmapi-text": Case("jmapi-text", "jmapi", False),
        "libtv-text": Case("libtv-text", "libtv", False),
        "jmapi-video": Case("jmapi-video", "jmapi", True),
        "libtv-video": Case("libtv-video", "libtv", True),
        "auto-text": Case("auto-text", "auto", False),
    }
    cases = [known[name.strip()] for name in args.cases.split(",") if name.strip()]
    if any(case.with_video for case in cases) and not reference_video:
        raise SystemExit("HEALTH_MONITOR_PROBE_VIDEO_URL is required for video cases")

    headers = {"Authorization": f"Bearer {settings.service_token}"}
    submitted: dict[str, tuple[Case, float]] = {}
    results: list[dict[str, object]] = []
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as client:
        for case in cases:
            request = {
                "model": "seedance-2.0",
                "channel": case.channel,
                "prompt": (
                    "A clean studio product shot of a blue glass bottle, slow camera push-in, "
                    "soft commercial lighting, realistic TV commercial style"
                ),
                "reference_image_urls": [],
                "reference_video_urls": [reference_video] if case.with_video else [],
                "reference_audio_urls": [],
                "duration_seconds": 5,
                "resolution": args.resolution,
                "aspect_ratio": "16:9",
                "sound": False,
                "external_ref": f"validation-{case.name}-{int(time.time())}",
                "metadata": {"purpose": "general-video-regression"},
            }
            started = time.monotonic()
            response = client.post("/v1/video-generations/jobs", json=request)
            response.raise_for_status()
            job_id = str(response.json()["job_id"])
            submitted[job_id] = (case, started)
            print(json.dumps({"case": case.name, "job_id": job_id, "status": "queued"}))

        deadline = time.monotonic() + args.timeout
        pending = set(submitted)
        while pending and time.monotonic() < deadline:
            for job_id in list(pending):
                case, started = submitted[job_id]
                response = client.get(f"/v1/video-generations/jobs/{job_id}")
                response.raise_for_status()
                body = response.json()
                status = str(body.get("status") or "")
                if status not in TERMINAL_STATES:
                    continue
                urls = [safe_url(str(url)) for url in body.get("result_urls") or []]
                item = {
                    "case": case.name,
                    "job_id": job_id,
                    "status": status,
                    "stage": body.get("stage"),
                    "channel": body.get("channel"),
                    "elapsed_seconds": body.get("elapsed_seconds"),
                    "wall_seconds": round(time.monotonic() - started, 3),
                    "result_urls": urls,
                    "error": body.get("error"),
                }
                results.append(item)
                pending.remove(job_id)
                print(json.dumps(item, ensure_ascii=False))
            if pending:
                time.sleep(args.poll_seconds)

    if pending:
        for job_id in sorted(pending):
            case, started = submitted[job_id]
            results.append(
                {
                    "case": case.name,
                    "job_id": job_id,
                    "status": "validation_timeout",
                    "wall_seconds": round(time.monotonic() - started, 3),
                }
            )
    passed = all(
        item.get("status") == "succeeded" and item.get("result_urls")
        for item in results
    ) and len(results) == len(cases)
    print(json.dumps({"passed": passed, "results": results}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
