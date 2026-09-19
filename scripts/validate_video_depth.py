from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx


def load_service_token(env_path: Path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("SERVICE_TOKEN="):
            return line.split("=", 1)[1]
    raise RuntimeError("SERVICE_TOKEN is missing")


def gpu_sample(gpu_id: int) -> tuple[int, int]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            f"--id={gpu_id}",
            "--query-gpu=memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    values = completed.stdout.strip().split(",")
    if len(values) != 2:
        return 0, 0
    return int(values[0].strip()), int(values[1].strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--gpu", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    token = load_service_token(Path("/home/donxu/ai-centre/.env"))
    headers = {"Authorization": f"Bearer {token}"}
    endpoint = "http://127.0.0.1:8320/v1/video-depth/jobs"
    source = (
        "https://raw.githubusercontent.com/DepthAnything/Video-Depth-Anything/"
        "main/assets/example_videos/davis_rollercoaster.mp4"
    )
    job_ids: list[str] = []
    results: dict[str, dict] = {}
    peak_memory = 0
    peak_utilization = 0
    with httpx.Client(timeout=30) as client:
        for index in range(args.concurrency):
            response = client.post(
                endpoint,
                headers=headers,
                json={
                    "source_uri": source,
                    "filename": f"depth-concurrency-{index + 1}.mp4",
                    "input_size": 518,
                    "max_resolution": 960,
                    "target_fps": 10,
                },
            )
            response.raise_for_status()
            job_ids.append(response.json()["job_id"])

        started = time.perf_counter()
        while time.perf_counter() - started < args.timeout:
            memory, utilization = gpu_sample(args.gpu)
            peak_memory = max(peak_memory, memory)
            peak_utilization = max(peak_utilization, utilization)
            for job_id in job_ids:
                payload = client.get(f"{endpoint}/{job_id}", headers=headers).json()
                if payload.get("status") in {"succeeded", "failed", "cancelled"}:
                    results[job_id] = payload
            if len(results) == len(job_ids):
                break
            time.sleep(0.25)

    print(
        json.dumps(
            {
                "wall_seconds": round(time.perf_counter() - started, 3),
                "peak_gpu_memory_mib": peak_memory,
                "peak_gpu_utilization_percent": peak_utilization,
                "results": [results.get(job_id) for job_id in job_ids],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
