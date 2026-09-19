from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import imageio_ffmpeg


def upload(path: Path, stage: str) -> str:
    upload_url = os.environ["KERNEL_UPLOAD_URL"]
    token = os.environ["KERNEL_API_TOKEN"]
    with path.open("rb") as stream:
        response = httpx.post(
            upload_url,
            headers={"Authorization": f"Bearer {token}"},
            data={
                "external_ref": f"h3-validation-{int(time.time())}",
                "run_id": "",
                "campaign_id": "",
                "project_id": "",
                "stage": stage,
                "actor": "h3-production-validation",
            },
            files={"file": (path.name, stream, "application/octet-stream")},
            timeout=httpx.Timeout(300, connect=30),
        )
    response.raise_for_status()
    uri = str(response.json()["uri"])
    if not uri.startswith("https://"):
        raise RuntimeError("upload did not return an HTTPS URI")
    return uri


def prepare(source: Path, work_dir: Path) -> tuple[Path, Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    short = work_dir / "reference-short-8s.mp4"
    identity = work_dir / "identity.png"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
         "-t", "8", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart", str(short)],
        check=True,
    )
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1", "-i", str(source),
         "-frames:v", "1", str(identity)],
        check=True,
    )
    return short, identity


def submit(client: httpx.Client, payload: dict) -> str:
    response = client.post("/v1/video-generations/minimax-h3/jobs", json=payload)
    response.raise_for_status()
    return str(response.json()["job_id"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=7200)
    args = parser.parse_args()
    short, identity = prepare(args.source, args.work_dir)
    short_url = upload(short, "validation.minimax_h3.reference_short")
    full_url = upload(args.source, "validation.minimax_h3.reference_full")
    identity_url = upload(identity, "validation.minimax_h3.identity")

    service_token = os.environ["SERVICE_TOKEN"]
    with httpx.Client(
        base_url="http://127.0.0.1:8320",
        headers={"Authorization": f"Bearer {service_token}"},
        timeout=60,
    ) as client:
        single_id = submit(client, {
            "reference_video_url": short_url,
            "identity_image_url": identity_url,
            "segment_mode": "single",
            "prompts": ["Use <Picture 1>, <Video 1> and <Audio 1> as references. Recreate the reference shot, action, camera movement, timing and soundtrack as coherent realistic live-action video. Keep the same person identity and preserve the original action rhythm."],
            "width": 416, "height": 736, "seed": 482901731,
            "external_ref": "h3-validation-single", "metadata": {"validation": True},
        })
        two_part_id = submit(client, {
            "reference_video_url": full_url,
            "identity_image_url": identity_url,
            "segment_mode": "two_part",
            "prompts": [
                "Use <Picture 1>, <Video 1> and <Audio 1> as references. Recreate this reference segment as coherent realistic live-action video, preserving its action, camera movement, timing and soundtrack while keeping the same person identity.",
                "Use <Picture 1>, <Video 1> and <Audio 1> as references. Recreate this reference segment as coherent realistic live-action video, preserving its action, camera movement, timing and soundtrack while keeping the same person identity.",
            ],
            "split_seconds": 12, "width": 416, "height": 736, "seed": 482901731,
            "external_ref": "h3-validation-two-part", "metadata": {"validation": True},
        })
        print(json.dumps({"single_job_id": single_id, "two_part_job_id": two_part_id}, ensure_ascii=False), flush=True)
        pending = {single_id, two_part_id}
        deadline = time.monotonic() + args.timeout
        last: dict[str, tuple[str, str, int]] = {}
        results: dict[str, dict] = {}
        while pending and time.monotonic() < deadline:
            for job_id in list(pending):
                response = client.get(f"/v1/video-generations/minimax-h3/jobs/{job_id}")
                response.raise_for_status()
                payload = response.json()
                signature = (str(payload["status"]), str(payload["stage"]), int(payload["progress"]))
                if last.get(job_id) != signature:
                    print(json.dumps({"job_id": job_id, **dict(zip(("status", "stage", "progress"), signature))}, ensure_ascii=False), flush=True)
                    last[job_id] = signature
                if payload["status"] in {"succeeded", "failed", "cancelled"}:
                    results[job_id] = payload
                    pending.remove(job_id)
            if pending:
                time.sleep(10)
        if pending:
            raise TimeoutError(f"validation jobs did not finish: {sorted(pending)}")
        print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
        if any(item["status"] != "succeeded" for item in results.values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
