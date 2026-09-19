from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

from control_plane.config import get_settings


MODELS = [
    "gpt-image-2",
    "gpt-image-2.5",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "nano-banana-2",
]
CASES = {
    "tvc_storyboard": (
        "16:9",
        "高端护肤精华液TVC广告关键帧，透明玻璃精华瓶立在浅水镜面中央，清晨金色侧逆光，一位30岁亚洲女性从柔焦背景走近产品，水波与微小光斑，真实商业摄影，电影级构图，皮肤质感自然，瓶身标签清晰，无文字叠加",
    ),
    "character": (
        "3:4",
        "高端护肤品牌TVC女主角定妆照，30岁亚洲女性，黑色长发低马尾，米白色真丝衬衫，温暖自信的表情，真实皮肤毛孔与细微纹理，柔和窗光，中性浅灰背景，85毫米商业人像摄影，不塑料，不油腻",
    ),
    "asset": (
        "1:1",
        "高端透明玻璃精华液产品资产图，磨砂银色滴管盖，瓶内淡金色液体，纯白到浅蓝渐变背景，产品居中，标签区域干净清晰，真实玻璃折射与细微水珠，影棚级商业摄影，边缘锐利，无额外物体，无文字",
    ),
    "environment": (
        "16:9",
        "高端护肤TVC环境空镜，现代极简浴室，浅色洞石墙面，大面积晨光穿过薄纱窗帘，台面有柔和水光反射与少量绿植，真实建筑摄影，电影级层次，材质细节丰富，无人物，无产品，无文字",
    ),
}


def main() -> None:
    settings = get_settings()
    base = "http://127.0.0.1:8320"
    headers = {"Authorization": f"Bearer {settings.service_token}"}
    output_dir = Path("/home/donxu/ai-centre/runtime/validation/grsai-five-model-tvc-20260909")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "results.json"
    retry_failed = "--retry-failed" in sys.argv and output_path.exists()
    results: list[dict] = json.loads(output_path.read_text(encoding="utf-8")) if retry_failed else []
    with httpx.Client(timeout=30) as client:
        requests = []
        if retry_failed:
            requests = [(item["model"],item["case"],item["aspect_ratio"],item["prompt"],item) for item in results if item.get("status") != "succeeded"]
        else:
            requests = [(model,case,ratio,prompt,None) for model in MODELS for case,(ratio,prompt) in CASES.items()]
        for model, case, ratio, prompt, old in requests:
            payload = {
                "model": model,
                "channel": "grsai",
                "prompt": prompt,
                "aspect_ratio": ratio,
                "image_size": "1K",
                "reference_image_urls": [],
                "metadata": {"experiment": "grsai-five-model-tvc-20260909", "case": case},
            }
            response = client.post(f"{base}/v1/image-generations/jobs", headers=headers, json=payload)
            response.raise_for_status()
            fresh={"model": model, "case": case, "job_id": response.json()["job_id"], **payload}
            if old is None: results.append(fresh)
            else: old.clear(); old.update(fresh)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"submitted {len(requests)}", flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        terminal = 0
        with httpx.Client(timeout=20) as client:
            for item in results:
                job = client.get(f"{base}/v1/image-generations/jobs/{item['job_id']}", headers=headers).json()
                item.update({key: job.get(key) for key in ("status", "elapsed_seconds", "result_urls", "error")})
                terminal += item["status"] in {"succeeded", "failed", "cancelled"}
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"terminal {terminal} of {len(results)}", flush=True)
        if terminal == len(results):
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
