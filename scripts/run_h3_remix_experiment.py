from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

from control_plane.h3_workflow import ComfyH3Client, build_prompt_graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=864)
    parser.add_argument("--seed", type=int, default=582904731)
    parser.add_argument("--prompt-profile", choices=("commercial", "natural"), default="commercial")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--no-turbo", action="store_true")
    args = parser.parse_args()

    prompt = (
        "[video edit] Preserve the exact 10-second camera movement, framing, timing, "
        "body motion, head turn, facial performance, and shot continuity from <Video 1>. "
        "Replace the foreground person completely with the fictional North American female technology "
        "presenter from <Picture 1>. <Picture 1> is the authoritative identity, face, hair, wardrobe, "
        "and body reference and must override the identity in <Video 1>. Replace all background people "
        "with a diverse North American product "
        "launch audience in contemporary business attire. Replace the ornate mansion with a premium "
        "modern Silicon Valley technology showroom, cool daylight, pale blue LED accents, glass and "
        "brushed aluminum surfaces, large abstract product displays without readable text or logos. "
        "Keep realistic skin texture, natural eyes and hands, coherent reflections, stable identity, "
        "sharp facial details, commercial advertising quality. Preserve the source audio from <Video 1>."
    )
    if args.prompt_profile == "natural":
        prompt = (
            "[video edit] Use <Video 1> as the exact source for the 10-second camera movement, framing, "
            "timing, body motion, head turn, facial performance, and shot continuity. Replace the foreground "
            "person with the same fictional North American woman shown in <Picture 1>; preserve her facial "
            "geometry, age, brown hair, pale blue business suit, white blouse, and natural body proportions. "
            "Use <Picture 1> only as identity and wardrobe reference, while matching the target shot's physically "
            "plausible lighting. Replace the background with a real occupied Silicon Valley product showroom, "
            "glass and brushed aluminum surfaces, restrained pale-blue practical lights, and a diverse audience "
            "in ordinary contemporary business clothing. Render as observational live-action camera footage: "
            "soft side daylight, neutral color science, moderate contrast, natural exposure roll-off, realistic "
            "skin pores and fine facial hairs, subtle under-eye texture and small skin-tone variations, restrained "
            "specular highlights, matte natural skin, realistic fabric weave and wrinkles, natural teeth and eyes. "
            "Avoid beauty lighting, ring-light catchlights, oily or wet skin, glossy forehead and cheeks, waxy or "
            "plastic skin, airbrushing, skin smoothing, HDR, bloom, over-sharpening, stock-photo perfection, CGI, "
            "3D-render appearance, perfect symmetry, and readable text or logos. Preserve the source audio from "
            "<Video 1>."
        )
    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    remote_name = f"ai-centre-h3-remix-{uuid4().hex[:10]}.mp4"
    started = time.monotonic()
    samples: list[dict] = []
    with ComfyH3Client(args.base_url, timeout_seconds=120) as client:
        uploaded = client.upload(args.input, remote_name)
        identity_name = None
        if args.identity:
            identity_name = client.upload(args.identity, f"ai-centre-h3-identity-{uuid4().hex[:10]}.png")
        graph = build_prompt_graph(
            uploaded, prompt, 10.0, args.width, args.height, args.seed,
            f"ai-centre/h3-remix-{uuid4().hex[:8]}", reference_has_audio=True,
            reference_image_name=identity_name,
        )
        graph["123"]["inputs"]["sampler_name"] = "res_multistep"
        graph["124"]["inputs"]["steps"] = args.steps
        if args.no_turbo:
            graph["142"]["inputs"]["model"] = ["127", 0]
        submission = client.client.post("/prompt", json={"prompt": graph, "client_id": uuid4().hex})
        if not submission.is_success:
            raise RuntimeError(f"worker rejected graph: {submission.status_code} {submission.text[:4000]}")
        submitted = submission.json()
        if submitted.get("node_errors"):
            raise RuntimeError(f"worker node errors: {json.dumps(submitted['node_errors'], ensure_ascii=False)}")
        prompt_id = submitted["prompt_id"]
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps({
            "prompt_id": prompt_id,
            "status": "submitted",
            "submitted_at_unix": time.time(),
            "output": str(args.output),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        def sample() -> None:
            try:
                response = httpx.get(
                    args.base_url.rstrip("/") + "/system_stats",
                    verify=False,
                    timeout=5,
                    trust_env=False,
                )
                device = response.json()["devices"][0]
                samples.append({
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "vram_total": device.get("vram_total"),
                    "vram_free": device.get("vram_free"),
                })
            except Exception:
                pass

        client.wait_and_download(prompt_id, args.output, 5, 1800, sample)
    metrics = {
        "prompt_id": prompt_id,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "input": str(args.input),
        "identity": str(args.identity) if args.identity else None,
        "output": str(args.output),
        "prompt": prompt,
        "configuration": {
            "mode": "ref2va video remix",
            "steps": args.steps,
            "turbo_lora": not args.no_turbo,
            "sampler": "res_multistep",
            "scheduler": "simple",
            "width": args.width,
            "height": args.height,
            "duration_seconds": 10,
            "seed": args.seed,
            "prompt_profile": args.prompt_profile,
        },
        "resource_samples": samples,
    }
    args.metrics.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
