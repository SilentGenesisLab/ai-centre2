from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from control_plane.config import get_settings
from control_plane.h3_store import H3Store


TERMINAL = {"succeeded", "failed", "cancelled"}
DEFAULT_VIDEO_URL = (
    "https://oss-imgai.sligenai.cn/ai-video-kernel/20260901/"
    "9aa9ff51320e46d095e5374421eb222a.mp4"
)


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    response = client.request(method, path, headers=headers, json=payload)
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:500]}
    return response.status_code, body


def base_payload() -> dict[str, Any]:
    return {
        "segment_mode": "single",
        "prompts": ["一只雄鹰在黑夜中飞过雪山，电影级光影"],
        "duration_seconds": 2,
        "resolution": "480p",
        "aspect_ratio": "9:16",
        "seed": 482901731,
        "metadata": {"purpose": "h3-benchmark"},
    }


def prepare_image(settings: Any, work_dir: Path, video_url: str) -> str:
    source = work_dir / "reference.mp4"
    frame = work_dir / "reference.png"
    if not source.is_file():
        with httpx.stream("GET", video_url, timeout=120, follow_redirects=True, trust_env=False) as response:
            response.raise_for_status()
            with source.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
    if not frame.is_file():
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "1", "-i", str(source), "-frames:v", "1", "-vf", "scale=480:-2", str(frame),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not frame.is_file():
            raise RuntimeError(f"reference frame extraction failed: {completed.stderr[-500:]}")
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    with frame.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data={
                "external_ref": "h3-benchmark-reference",
                "run_id": "",
                "campaign_id": "",
                "project_id": "",
                "stage": "validation.h3.reference_image",
                "actor": "h3-benchmark",
            },
            files={"file": (frame.name, stream, "image/png")},
            timeout=httpx.Timeout(120, connect=20),
        )
    response.raise_for_status()
    uri = str(response.json()["uri"])
    if not uri.startswith("https://"):
        raise RuntimeError("image upload did not return an HTTPS URL")
    return uri


def cancel_job(client: httpx.Client, headers: dict[str, str], job_id: str) -> int:
    status, _ = request_json(
        client, "POST", f"/v1/video-generations/minimax-h3/jobs/{job_id}/cancel", headers
    )
    return status


def validation_matrix(client: httpx.Client, headers: dict[str, str]) -> list[dict[str, Any]]:
    cases: list[tuple[str, dict[str, Any], int, bool]] = []
    for name, updates, expected, cancel in (
        ("duration_below_min", {"duration_seconds": 1.99}, 422, False),
        ("duration_at_min", {"duration_seconds": 2}, 202, True),
        ("duration_at_max", {"duration_seconds": 15}, 202, True),
        ("duration_above_max", {"duration_seconds": 15.01}, 422, False),
        ("empty_prompt", {"prompts": ["   "]}, 422, False),
        ("single_two_prompts", {"prompts": ["one", "two"]}, 422, False),
        ("two_part_without_video", {"segment_mode": "two_part", "prompts": ["one", "two"], "duration_seconds": None}, 422, False),
        ("unsupported_resolution", {"resolution": "2k"}, 422, False),
        ("unsupported_aspect_ratio", {"aspect_ratio": "21:9"}, 422, False),
        ("negative_seed", {"seed": -1}, 422, False),
        ("seed_at_max", {"seed": 18_446_744_073_709_551_615}, 202, True),
        ("seed_above_max", {"seed": 18_446_744_073_709_551_616}, 422, False),
        ("non_https_reference", {"reference_image_url": "http://example.com/reference.png"}, 422, False),
    ):
        payload = {**base_payload(), **updates}
        if payload.get("duration_seconds") is None:
            payload.pop("duration_seconds", None)
        cases.append((name, payload, expected, cancel))

    results: list[dict[str, Any]] = []
    for name, payload, expected, should_cancel in cases:
        started = time.perf_counter()
        status, body = request_json(client, "POST", "/v1/video-generations/minimax-h3/jobs", headers, payload)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        job_id = body.get("job_id") if isinstance(body, dict) else None
        cancel_status = cancel_job(client, headers, str(job_id)) if should_cancel and job_id else None
        results.append({
            "name": name,
            "expected_http": expected,
            "actual_http": status,
            "passed": status == expected and (not should_cancel or cancel_status == 200),
            "latency_ms": elapsed_ms,
            "job_id": job_id,
            "cancel_http": cancel_status,
            "detail": body.get("detail") if status >= 400 and isinstance(body, dict) else None,
        })
    return results


def submit_case(
    base_url: str,
    headers: dict[str, str],
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client:
        status, body = request_json(client, "POST", "/v1/video-generations/minimax-h3/jobs", headers, payload)
    return {
        "name": name,
        "submit_http": status,
        "submit_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "submitted_at": started_wall,
        "job_id": body.get("job_id") if isinstance(body, dict) else None,
        "submit_body": body if status >= 400 else None,
    }


def worker_sample(store: H3Store) -> dict[str, Any]:
    workers = store.list_workers(public=False)
    online = next((worker for worker in workers if worker.get("status") in {"online", "busy"}), None)
    if not online:
        return {"sampled_at": datetime.now(timezone.utc).isoformat(), "online": False}
    total = int(online.get("vram_total_bytes") or 0)
    free = int(online.get("vram_free_bytes") or 0)
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "online": True,
        "status": online.get("status"),
        "queue_running": online.get("queue_running"),
        "queue_pending": online.get("queue_pending"),
        "current_job": bool(online.get("current_job_id")),
        "vram_total_gib": round(total / 2**30, 3) if total else None,
        "vram_used_gib": round((total - free) / 2**30, 3) if total else None,
    }


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def finalize_metrics(result: dict[str, Any], job: dict[str, Any]) -> None:
    result["final"] = job
    created = parse_time(job.get("created_at"))
    finished = parse_time(job.get("finished_at"))
    attempts = job.get("attempts") or []
    attempt_started = parse_time(attempts[0].get("started_at")) if attempts else None
    result["queue_wait_seconds"] = (
        round((attempt_started - created).total_seconds(), 3) if created and attempt_started else None
    )
    result["wall_seconds"] = (
        round((finished - created).total_seconds(), 3) if created and finished else None
    )
    result["worker_elapsed_seconds"] = job.get("elapsed_seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark MiniMax H3 API without exposing credentials")
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--video-url", default=DEFAULT_VIDEO_URL)
    parser.add_argument("--output", default="runtime/validation/h3-benchmark-20260902/result.json")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    args = parser.parse_args()

    token = os.environ.get("SERVICE_TOKEN")
    if not token:
        raise RuntimeError("SERVICE_TOKEN is required")
    headers = {"Authorization": f"Bearer {token}"}
    settings = get_settings()
    store = H3Store(settings.h3_db_path)
    active = [
        job for job in store.list_jobs(limit=100)["items"]
        if job.get("status") in {"queued", "running", "cancel_requested"}
    ]
    if active:
        raise RuntimeError(f"refusing to benchmark while {len(active)} H3 job(s) are active")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image_url = prepare_image(settings, output.parent, args.video_url)
    started_at = datetime.now(timezone.utc).isoformat()
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        validations = validation_matrix(client, headers)
    print("VALIDATION", sum(item["passed"] for item in validations), "/", len(validations), flush=True)

    cases = {
        "text_only": {
            **base_payload(),
            "prompts": ["海面日出，云层被金色阳光照亮，镜头缓慢向前推进，电影感"],
            "external_ref": "h3-benchmark-text-20260902",
        },
        "image_reference": {
            **base_payload(),
            "reference_image_url": image_url,
            "prompts": ["<Picture 1>中的人物自然抬头看向远方，微风吹动头发，镜头稳定"],
            "external_ref": "h3-benchmark-image-20260902",
        },
        "video_reference": {
            **base_payload(),
            "reference_video_url": args.video_url,
            "prompts": ["保持<Video 1>的主体动作和镜头运动，画面自然连贯"],
            "external_ref": "h3-benchmark-video-20260902",
        },
    }
    submitted: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(submit_case, args.base_url, headers, name, payload): name
            for name, payload in cases.items()
        }
        for future in as_completed(futures):
            result = future.result()
            print("SUBMITTED", result["name"], result["submit_http"], result["job_id"], flush=True)
            submitted.append(result)
    submitted.sort(key=lambda item: item["name"])
    if any(item["submit_http"] != 202 or not item["job_id"] for item in submitted):
        raise RuntimeError("one or more benchmark submissions failed")

    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + args.timeout_seconds
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        while time.monotonic() < deadline:
            complete = True
            states: list[str] = []
            for item in submitted:
                if item.get("final"):
                    states.append(f"{item['name']}={item['final']['status']}")
                    continue
                status, job = request_json(
                    client, "GET", f"/v1/video-generations/minimax-h3/jobs/{item['job_id']}", headers
                )
                if status != 200:
                    complete = False
                    states.append(f"{item['name']}=http{status}")
                    continue
                states.append(f"{item['name']}={job.get('status')}:{job.get('stage')}:{job.get('progress')}%")
                if job.get("status") in TERMINAL:
                    finalize_metrics(item, job)
                else:
                    complete = False
            sample = worker_sample(store)
            samples.append(sample)
            print(
                "PROGRESS", datetime.now(timezone.utc).isoformat(), " | ".join(states),
                "worker=", sample.get("status"), "vram_used_gib=", sample.get("vram_used_gib"),
                flush=True,
            )
            if complete:
                break
            time.sleep(args.poll_seconds)
        else:
            for item in submitted:
                if not item.get("final") and item.get("job_id"):
                    cancel_job(client, headers, str(item["job_id"]))
            raise TimeoutError("benchmark timed out; unfinished jobs were cancelled")

    payload = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "worker_count": len(store.list_workers(public=False)),
        "online_worker_count": len([
            worker for worker in store.list_workers(public=False)
            if worker.get("status") in {"online", "busy"}
        ]),
        "validation": validations,
        "functional": submitted,
        "worker_samples": samples,
        "reference_video_url": args.video_url,
        "reference_image_url": image_url,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT", output, flush=True)
    return 0 if all(item["final"]["status"] == "succeeded" for item in submitted) else 2


if __name__ == "__main__":
    raise SystemExit(main())
