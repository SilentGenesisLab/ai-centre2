"""gpt-image-2.6 两档 × grsai/teamorouter 的生成对比。

**在生产服务器上跑**：它直接读能力库、解密渠道凭据，用中台 `_payload` 会拼出的
同一份请求体打上游，然后把图落盘。绕开 HTTP 是为了不依赖发版 —— 生产上有未入库的
改动压着，不能重启 control/worker。

用法（仓库根目录）：
    .venv/bin/python scripts/run_gpt_image_26_comparison.py            # 含 2.5 基线
    .venv/bin/python scripts/run_gpt_image_26_comparison.py --only 2.6 # 只跑 2.6
    .venv/bin/python scripts/run_gpt_image_26_comparison.py --retry-failed
    # 分辨率扫描：只打 16:9 这一个 case，跑 2K/4K
    .venv/bin/python scripts/run_gpt_image_26_comparison.py \
        --image-sizes 2K,4K --cases tvc_storyboard --out gpt-image-26-res-20261109
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control_plane.config import get_settings
from control_plane.generation_tasks import (
    PIXEL_ASPECTS,
    _response_body,
    _result,
    _state,
    _task_id,
)

DB_PATH = Path("/home/donxu/ai-centre/runtime/ai-capabilities/capabilities.db")
OUT_DIR = Path("/home/donxu/ai-centre/runtime/validation/gpt-image-26-20261109")
CHANNELS = ("grsai", "teamorouter")
TARGETS = ["gpt-image-2.6-sunburst", "gpt-image-2.6-flare"]
BASELINES = ["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"]
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


def channel_credentials() -> dict[str, dict]:
    key = hashlib.sha256(("ai-capabilities:" + get_settings().service_token).encode()).digest()
    marks = ",".join("?" * len(CHANNELS))
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT code,adapter,base_url,auth_type,credential_enc FROM channels WHERE code IN ({marks})",
            CHANNELS,
        ).fetchall()
    out = {}
    for row in rows:
        encrypted = row["credential_enc"] or ""
        credential = ""
        if encrypted:
            raw = base64.b64decode(encrypted)
            credential = AESGCM(key).decrypt(raw[:12], raw[12:], b"channel-secret").decode()
        out[row["code"]] = {
            "adapter": row["adapter"],
            "base_url": row["base_url"].rstrip("/"),
            "auth_type": row["auth_type"],
            "credential": credential,
        }
        # 只报长度，不打印凭据本身
        print(f"channel {row['code']}: base={row['base_url']} cred_len={len(credential)}", flush=True)
    return out


def headers(channel: dict) -> dict[str, str]:
    if channel["auth_type"] == "none" or not channel["credential"]:
        return {}
    if channel["auth_type"] == "authorization":
        return {"Authorization": channel["credential"]}
    return {"Authorization": f"Bearer {channel['credential']}"}


def image_size_of(path: Path) -> str | None:
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return f"{width}x{height}"
    if data[:2] == b"\xff\xd8":
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return f"{width}x{height}"
            index += 2 + struct.unpack(">H", data[index + 2:index + 4])[0]
    return None


def save(url_or_b64: str, stem: str, meta: dict) -> dict:
    meta = dict(meta)
    if url_or_b64.startswith("http"):
        meta["source"] = "url"
        response = httpx.get(url_or_b64, timeout=120)
        response.raise_for_status()
        data = response.content
    else:
        meta["source"] = "b64"
        data = base64.b64decode(url_or_b64)
    suffix = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
    path = OUT_DIR / f"{stem}{suffix}"
    path.write_bytes(data)
    meta.update(file=path.name, bytes=len(data), pixels=image_size_of(path))
    return meta


def opt(name: str, default: str) -> str:
    """`--name value` 取值；缺省返回 default。"""
    if name in sys.argv:
        index = sys.argv.index(name) + 1
        if index < len(sys.argv):
            return sys.argv[index]
    return default


def run_grsai(client: httpx.Client, channel: dict, model: str, ratio: str, prompt: str,
              image_size: str = "1K") -> list[str]:
    """grsai 的 /v1/draw/completions 是**同步 SSE**：提交那次连接会挂住 ~30s，
    回来时 `data: {...}` 里已经是成品（status=succeeded + results[].url）。
    2.5 也是这个样子，所以不必先提交再查询 —— 只有它没给结果时才回落到轮询。
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "aspectRatio": PIXEL_ASPECTS[image_size][ratio],
        "urls": [],
        "shutProgress": True,
    }
    response = client.post(
        urljoin(channel["base_url"] + "/", "v1/draw/completions"),
        headers=headers(channel), json=payload, timeout=300,
    )
    response.raise_for_status()
    body = _response_body(response)
    if _state(body, "grsai") in {"succeeded", "success", "completed"}:
        urls = _result(body)
        if urls:
            return urls
    task_id = _task_id(body)
    if not task_id:
        raise RuntimeError(f"grsai 没给任务号: {json.dumps(body, ensure_ascii=False)[:300]}")
    return poll_grsai(client, channel, task_id)


def poll_grsai(client: httpx.Client, channel: dict, task_id: str) -> list[str]:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        response = client.post(
            urljoin(channel["base_url"] + "/", "v1/draw/result"),
            headers=headers(channel), json={"id": task_id},
        )
        response.raise_for_status()
        body = _response_body(response)
        state = _state(body, "grsai")
        if state in {"succeeded", "success", "completed"}:
            urls = _result(body)
            if urls:
                return urls
        if state in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"grsai 生成失败: {json.dumps(body, ensure_ascii=False)[:300]}")
        time.sleep(10)
    raise TimeoutError("grsai 轮询超时")


def submit_teamorouter(client: httpx.Client, channel: dict, model: str, ratio: str, prompt: str,
                       image_size: str = "1K") -> list[str]:
    size = PIXEL_ASPECTS[image_size][ratio]
    response = client.post(
        urljoin(channel["base_url"] + "/", "v1/images/generations"),
        headers=headers(channel),
        json={"model": model, "prompt": prompt, "n": 1, "size": size},
        timeout=300,
    )
    response.raise_for_status()
    body = response.json()
    items = body.get("data") or []
    if not items:
        raise RuntimeError(f"teamorouter 没回 data: {response.text[:300]}")
    item = items[0]
    if item.get("b64_json"):
        return [item["b64_json"]]
    if item.get("url"):
        return [item["url"]]
    raise RuntimeError(f"teamorouter 响应里既没有 b64 也没有 url: {response.text[:300]}")


def main() -> None:
    only = opt("--only", "")
    if only == "2.6":
        models = TARGETS
    elif only == "2.5":
        models = BASELINES
    elif only:
        models = [only]
    else:
        models = TARGETS + BASELINES
    image_sizes = [s for s in opt("--image-sizes", "1K").split(",") if s]
    case_names = [c for c in opt("--cases", ",".join(CASES)).split(",") if c]
    wanted_channels = [c for c in opt("--channels", ",".join(CHANNELS)).split(",") if c]

    out_dir = OUT_DIR.parent / opt("--out", OUT_DIR.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "results.json"
    retry_failed = "--retry-failed" in sys.argv and output_path.exists()
    results: list[dict] = json.loads(output_path.read_text(encoding="utf-8")) if retry_failed else []
    channels = channel_credentials()

    def stem_of(channel: str, model: str, case: str, size: str) -> str:
        # 1K 沿用原来的命名，2K/4K 带上档位，避免覆盖第一轮的产物
        return f"{channel}-{model}-{case}" + ("" if size == "1K" else f"-{size}")

    jobs = [
        (channel, model, case, CASES[case][0], CASES[case][1], size)
        for channel in wanted_channels
        if channel in channels
        for model in models
        for case in case_names
        for size in image_sizes
        if not retry_failed
        or not any(item["channel"] == channel and item["model"] == model and item["case"] == case
                    and item.get("image_size") == size and item.get("status") == "succeeded"
                    for item in results)
    ]
    print(f"running {len(jobs)} generations -> {out_dir}", flush=True)
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for index, (channel_code, model, case, ratio, prompt, size) in enumerate(jobs, start=1):
            stem = stem_of(channel_code, model, case, size)
            meta = {"channel": channel_code, "model": model, "case": case,
                    "aspect_ratio": ratio, "image_size": size, "prompt": prompt,
                    "requested_pixels": PIXEL_ASPECTS[size][ratio]}
            started = time.monotonic()
            try:
                if channel_code == "grsai":
                    outputs = run_grsai(client, channels[channel_code], model, ratio, prompt, size)
                else:
                    outputs = submit_teamorouter(client, channels[channel_code], model, ratio, prompt, size)
                meta.update(save(outputs[0], stem, meta))
                meta.update(status="succeeded", error=None)
            except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整批
                detail = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = " " + exc.response.text[:300].replace("\n", " ")
                meta.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:400] + detail)
            meta["elapsed_seconds"] = round(time.monotonic() - started, 1)
            results.append(meta)
            print(f"[{index}/{len(jobs)}] {stem} {meta['status']} {meta.get('pixels')} {meta['elapsed_seconds']}s"
                  + ("" if meta["status"] == "succeeded" else f" {meta['error']}"), flush=True)
            output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for item in results if item.get("status") == "succeeded")
    print(f"done: {ok}/{len(results)} succeeded -> {output_path}", flush=True)


if __name__ == "__main__":
    main()
