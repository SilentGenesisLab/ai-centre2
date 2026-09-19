from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from control_plane.h3_workflow import build_prompt_graph


def upload(client: httpx.Client, source: Path, remote_name: str) -> str:
    with source.open("rb") as stream:
        response = client.post(
            "/upload/image",
            files={"image": (remote_name, stream, "video/mp4")},
            data={"type": "input", "overwrite": "true"},
        )
    response.raise_for_status()
    return str(response.json()["name"])


def submit_and_wait(client: httpx.Client, graph: dict[str, Any], timeout: float) -> tuple[str, dict[str, Any], float]:
    started = time.monotonic()
    response = client.post("/prompt", json={"prompt": graph, "client_id": uuid4().hex})
    if response.is_error:
        raise RuntimeError(f"worker rejected prompt ({response.status_code}): {response.text[:4000]}")
    payload = response.json()
    if payload.get("node_errors"):
        raise RuntimeError(json.dumps(payload["node_errors"], ensure_ascii=False))
    prompt_id = str(payload["prompt_id"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history_response = client.get(f"/history/{prompt_id}")
        history_response.raise_for_status()
        history = history_response.json().get(prompt_id)
        if history:
            status = history.get("status") or {}
            if status.get("status_str") == "error" or status.get("completed") is False:
                raise RuntimeError(json.dumps(status, ensure_ascii=False))
            return prompt_id, history, time.monotonic() - started
        time.sleep(5)
    raise TimeoutError(f"H3 prompt {prompt_id} timed out")


def output_video(history: dict[str, Any]) -> dict[str, str]:
    for output in (history.get("outputs") or {}).values():
        for key in ("videos", "gifs", "images"):
            for item in output.get(key) or []:
                filename = str(item.get("filename") or "")
                if filename.lower().endswith((".mp4", ".webm", ".mov")):
                    return {
                        "filename": filename,
                        "subfolder": str(item.get("subfolder") or ""),
                        "type": str(item.get("type") or "output"),
                    }
    raise RuntimeError("workflow completed without a video output")


def download(client: httpx.Client, item: dict[str, str], target: Path) -> None:
    with client.stream("GET", "/view", params=item) as response:
        response.raise_for_status()
        with target.open("wb") as stream:
            for chunk in response.iter_bytes():
                stream.write(chunk)


def eight_step_graph(template: Path, video_name: str, prompt: str, seconds: float, seed: int) -> dict[str, Any]:
    graph = json.loads(template.read_text(encoding="utf-8"))
    graph = {key: value for key, value in graph.items() if isinstance(value, dict) and value.get("class_type")}
    graph["256"]["inputs"]["video"] = video_name
    graph["250"]["inputs"]["value"] = prompt
    graph["132"]["inputs"]["value"] = seconds
    graph["129"]["inputs"]["noise_seed"] = seed
    graph["184"]["inputs"]["value"] = 8
    inputs = graph["275"]["inputs"]
    for key in list(inputs):
        if key.startswith("ref_images.") or key.startswith("ref_audios."):
            inputs.pop(key)
    inputs["ref_videos.ref_video_0"] = ["264", 0]
    graph["180"]["inputs"]["filename_prefix"] = "video/h3-ab-8step"
    return graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--four-url", required=True)
    parser.add_argument("--eight-url", required=True)
    parser.add_argument("--eight-workflow", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--seed", type=int, default=482901731)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--mode", choices=("both", "4step", "8step"), default="both")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    targets = (("4step", args.four_url), ("8step", args.eight_url))
    for label, url in targets:
        if args.mode != "both" and args.mode != label:
            continue
        with httpx.Client(base_url=url.rstrip("/"), timeout=120, verify=False) as client:
            remote_name = f"h3-ab-{label}-{uuid4().hex[:8]}.mp4"
            upload(client, args.video, remote_name)
            if label == "4step":
                graph = build_prompt_graph(
                    remote_name, args.prompt, args.seconds, 480, 832, args.seed,
                    "h3-ab-4step", reference_has_audio=False,
                )
            else:
                graph = eight_step_graph(args.eight_workflow, remote_name, args.prompt, args.seconds, args.seed)
            prompt_id, history, elapsed = submit_and_wait(client, graph, args.timeout)
            item = output_video(history)
            target = args.output_dir / f"{label}.mp4"
            download(client, item, target)
            results[label] = {"prompt_id": prompt_id, "elapsed_seconds": round(elapsed, 3), "output": str(target)}
    (args.output_dir / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
