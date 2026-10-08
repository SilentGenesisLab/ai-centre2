from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .celery_app import celery_app
from .concurrency import job_slot, slot_wait_reporter
from .config import get_settings
from .media_fetch import IMAGE_MEDIA, download_public_media
from .seedvr2_inference import MODELS, SeedVR2Inference
from .video_upscale_tasks import _upload_file


_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE: tuple[str, SeedVR2Inference] | None = None


def resolve_model(requested: str, default: str) -> str:
    """把请求里的模型名落到白名单上。

    `auto` 用配置里的默认值。**非白名单一律报错**，不退回默认值 ——
    静默退回会让调用方以为自己在用 7B，其实一直在跑 3B。
    """
    name = default if requested == "auto" else requested
    if name not in MODELS:
        raise ValueError(f"unsupported image upscale model: {requested}")
    return name


def _get_model(name: str) -> SeedVR2Inference:
    """一次只在显存里留一个模型，换模型前先把旧的放掉（照 depth_tasks 的写法）。"""
    global _MODEL_CACHE

    settings = get_settings()
    with _MODEL_CACHE_LOCK:
        if _MODEL_CACHE is not None and _MODEL_CACHE[0] == name:
            return _MODEL_CACHE[1]
        if _MODEL_CACHE is not None:
            _MODEL_CACHE[1].close()
            _MODEL_CACHE = None
        loaded = SeedVR2Inference(
            settings.image_upscale_source_dir,
            settings.image_upscale_model_dir,
            MODELS[name],
        )
        _MODEL_CACHE = (name, loaded)
        return loaded


@celery_app.task(bind=True, name="control_plane.image_upscale", time_limit=7200, soft_time_limit=7140)
def upscale_images(self, request_data: dict[str, Any]) -> dict[str, Any]:
    with job_slot("image_upscale", on_wait=slot_wait_reporter(self)):
        return _upscale_images(self, request_data)


def _upscale_images(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    started = time.perf_counter()
    work_dir = settings.image_upscale_work_dir / job_id
    source_dir, result_dir = work_dir / "source", work_dir / "result"
    source_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    try:
        uris = [str(uri) for uri in request_data["source_uris"]]
        if not uris:
            raise ValueError("source_uris is empty")
        if len(uris) > settings.image_upscale_max_images:
            raise ValueError(f"too many images: {len(uris)} > {settings.image_upscale_max_images}")
        model_name = resolve_model(str(request_data.get("model", "auto")), settings.image_upscale_default_model)
        target_short_side = int(request_data.get("target_short_side") or settings.image_upscale_target_short_side)
        count = len(uris)
        results: list[dict[str, Any]] = [{"source_uri": uri, "status": "pending"} for uri in uris]

        def report(stage: str, base: int, span: int, done: int, **extra: Any) -> None:
            self.update_state(state="PROGRESS", meta={
                "stage": stage, "image_count": count, "completed_images": done,
                "progress": base + int(span * done / count), **extra,
            })

        # 先全部下载再统一推理。理由是**坏的 URL 不该白等一次模型加载**：
        # 一批 32 张里第 30 张是死链，如果边下边跑，前面 29 张的模型加载就白付了。
        # 而且这一步不下发进度的话，调用方在长批次里看不到任何东西。
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 2, "image_count": count, "completed_images": 0})
        downloaded: list[Path | None] = []
        for index, uri in enumerate(uris):
            try:
                item = download_public_media(
                    uri, source_dir, f"source-{index:03d}", IMAGE_MEDIA,
                    settings.image_upscale_max_download_bytes,
                    settings.image_upscale_download_timeout_seconds,
                )
                downloaded.append(item.path)
            except Exception as exc:  # noqa: BLE001 — 单张坏图不该拖垮整批
                downloaded.append(None)
                results[index] = {"source_uri": uri, "status": "failed",
                                  "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            report("downloading", 2, 8, index + 1)

        model = _get_model(model_name)
        for index, (uri, path) in enumerate(zip(uris, downloaded, strict=True)):
            if path is None:
                report("upscaling", 10, 85, index + 1)
                continue
            image_started = time.perf_counter()
            target = result_dir / f"{index:03d}.png"
            try:
                source_size, output_size = model.upscale(path, target, target_short_side)
                url = _upload_file(
                    target, job_id, "media.image_upscale", target.name,
                    content_type="image/png", actor="image-upscale-worker",
                )
                results[index] = {
                    "source_uri": uri, "status": "succeeded", "result_url": url,
                    "source_size": f"{source_size[0]}x{source_size[1]}",
                    "output_size": f"{output_size[0]}x{output_size[1]}",
                    "seconds": round(time.perf_counter() - image_started, 3),
                }
            except Exception as exc:  # noqa: BLE001
                results[index] = {"source_uri": uri, "status": "failed",
                                  "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            report("upscaling", 10, 85, index + 1)

        succeeded = [item for item in results if item["status"] == "succeeded"]
        failed = [item for item in results if item["status"] == "failed"]
        return {
            "job_id": job_id,
            # 单张失败不改作业状态：批量参考图里，一张坏图让整批「失败」会让调用方
            # 分不清「全挂了」和「挂了一张」。要看逐张结果就读 results。
            "status": "succeeded",
            "model": model_name,
            "model_file": MODELS[model_name],
            "target_short_side": target_short_side,
            "image_count": count,
            "succeeded_count": len(succeeded),
            "failed_count": len(failed),
            "result_urls": [item["result_url"] for item in succeeded],
            "results": results,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
