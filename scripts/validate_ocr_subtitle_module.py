#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

VALIDATION_LIBS = Path(__file__).resolve().parents[1] / "runtime" / "validation" / "python-libs"
if VALIDATION_LIBS.is_dir():
    sys.path.insert(0, str(VALIDATION_LIBS))

from PIL import Image, ImageDraw, ImageFont

from validate_module_common import (
    ApiClient,
    AssetStore,
    ResourceSampler,
    ffmpeg_bin,
    json_safe,
    load_env,
    mean,
    now_iso,
    percentile,
    resource_summary,
    run,
    text_errors,
    write_artifacts,
)


IMAGE_CASES = {
    "zh-clean": {"text": "人工智能让生活更美好 2026", "hint": "zh", "size": 58, "filter": ""},
    "en-clean": {"text": "Reliable OCR Test 2026", "hint": "en", "size": 58, "filter": ""},
    "mixed": {"text": "订单编号 AIC-2026-0818 金额 128.50 元", "hint": "zh", "size": 52, "filter": ""},
    "small-text": {"text": "小字号识别 Small Text 2026", "hint": "zh", "size": 28, "filter": ""},
    "noisy": {"text": "噪声环境识别 Noise 2026", "hint": "zh", "size": 52, "filter": ",noise=alls=10:allf=t+u"},
    "rotated": {"text": "轻微旋转文字 Rotate 2026", "hint": "zh", "size": 52, "filter": ",rotate=0.025:fillcolor=white"},
}


def ocr_texts(payload: dict[str, Any]) -> list[str]:
    return [
        str(item.get("text") or "")
        for result in payload.get("results") or []
        if isinstance(result, dict)
        for item in result.get("items") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]


def subtitle_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("events") or payload.get("subtitle_events") or []
    return [item for item in events if isinstance(item, dict)]


class Validator:
    def __init__(self, args: argparse.Namespace, env: dict[str, str]) -> None:
        self.args = args
        self.output = args.output_dir
        self.assets_dir = self.output / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("SERVICE_TOKEN") or env.get("SERVICE_TOKEN", "")
        self.public = ApiClient(args.base_url, token, args.timeout)
        self.internal = ApiClient(args.internal_url, token, args.timeout)
        self.store = AssetStore(env, f"ai-centre/validation/ocr-subtitle/{args.run_id}")
        self.results: list[dict[str, Any]] = []
        self.assets: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.public.close()
        await self.internal.close()

    def create_text_image(self, name: str, text: str, size: int, extra_filter: str = "") -> Path:
        target = self.assets_dir / f"{name}.png"
        image = Image.new("RGB", (1280, 720), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(self.args.font), size)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((1280 - box[2]) / 2, (720 - box[3]) / 2), text, font=font, fill="black")
        if "noise=" in extra_filter:
            array = np.asarray(image).astype(np.int16)
            noise = np.random.default_rng(20260818).normal(0, 10, array.shape)
            image = Image.fromarray(np.clip(array + noise, 0, 255).astype(np.uint8))
        if "rotate=" in extra_filter:
            image = image.rotate(1.5, resample=Image.Resampling.BICUBIC, fillcolor="white")
        image.save(target)
        return target

    async def prepare_assets(self) -> None:
        if not self.args.font.is_file():
            raise RuntimeError(f"font is missing: {self.args.font}")
        for name, case in IMAGE_CASES.items():
            path = self.create_text_image(name, case["text"], case["size"], case["filter"])
            self.assets[name] = {
                **self.store.upload(path, path.name, "image/png"),
                "expected": case["text"], "hint": case["hint"],
            }
        clean = self.assets_dir / "zh-clean.png"
        for suffix, content_type, codec in (
            ("JPG", "image/jpeg", ["-q:v", "3"]),
            ("webp", "image/webp", ["-quality", "85"]),
        ):
            target = self.assets_dir / f"zh-clean.{suffix}"
            run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(clean), *codec, str(target)])
            self.assets[f"format-{suffix}"] = {
                **self.store.upload(target, target.name, content_type),
                "expected": IMAGE_CASES["zh-clean"]["text"], "hint": "zh",
            }
        roi = self.assets_dir / "roi.png"
        image = Image.new("RGB", (1280, 720), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(self.args.font), 52)
        draw.text((100, 150), "上半区文字 TOP 2026", font=font, fill="black")
        draw.text((100, 510), "下半区文字 BOTTOM 0818", font=font, fill="black")
        image.save(roi)
        self.assets["roi"] = {
            **self.store.upload(roi, roi.name, "image/png"),
            "expected": "上半区文字 TOP 2026 下半区文字 BOTTOM 0818", "hint": "zh",
        }
        self.create_subtitle_video()
        safe_assets = {
            key: {item_key: item_value for item_key, item_value in value.items() if item_key != "url"}
            for key, value in self.assets.items()
        }
        (self.output / "fixtures.json").write_text(json.dumps(safe_assets, ensure_ascii=False, indent=2), encoding="utf-8")

    def create_subtitle_video(self) -> None:
        frames = []
        colors = ((32, 48, 96), (96, 48, 32), (32, 96, 64))
        font = ImageFont.truetype(str(self.args.font), 48)
        for index, (text, color) in enumerate(zip(("第一段字幕", "第二段字幕", "第三段字幕"), colors), start=1):
            image = Image.new("RGB", (1280, 720), color)
            draw = ImageDraw.Draw(image)
            box = draw.textbbox((0, 0), text, font=font, stroke_width=3)
            draw.text(((1280 - box[2]) / 2, 610), text, font=font, fill="white", stroke_width=3, stroke_fill="black")
            path = self.assets_dir / f"subtitle-{index}.png"
            image.save(path)
            frames.append(path)
        target = self.assets_dir / "subtitle-controlled.mp4"
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-t", "2", "-i", str(frames[0]),
            "-loop", "1", "-t", "2", "-i", str(frames[1]),
            "-loop", "1", "-t", "2", "-i", str(frames[2]),
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[video]",
            "-map", "[video]", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ])
        self.assets["subtitle-video"] = {
            "path": str(target), "bytes": target.stat().st_size,
            "expected_events": [
                {"text": "第一段字幕", "start": 0.0, "end": 2.0},
                {"text": "第二段字幕", "start": 2.0, "end": 4.0},
                {"text": "第三段字幕", "start": 4.0, "end": 6.0},
            ],
        }

    async def ocr_call(
        self,
        case_id: str,
        images: list[dict[str, Any]],
        expected: list[str],
        hint: str | None,
        phase: str,
        concurrency: int = 1,
        sample_index: int = 0,
    ) -> dict[str, Any]:
        payload = {"job_id": f"{self.args.run_id}-{case_id}-{sample_index}", "source_lang_hint": hint, "images": images}
        response, latency, error = await self.public.request("POST", "/v1/ocr/batch", json=payload)
        body: dict[str, Any] = {}
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                body = {}
        actuals = []
        scores = []
        for result in body.get("results") or []:
            items = result.get("items") or [] if isinstance(result, dict) else []
            actuals.append(" ".join(str(item.get("text") or "") for item in items if isinstance(item, dict)))
            scores.extend(float(item["score"]) for item in items if isinstance(item, dict) and isinstance(item.get("score"), (int, float)))
        errors = [text_errors(want, actuals[index] if index < len(actuals) else "")[0] for index, want in enumerate(expected)]
        result = {
            "case_id": case_id, "phase": phase, "sample_index": sample_index,
            "concurrency": concurrency, "images": len(images), "hint": hint,
            "http_status": response.status_code if response is not None else 0,
            "latency_seconds": round(latency, 4), "upstream_seconds": body.get("elapsed_seconds"),
            "engine": body.get("engine"), "model_version": body.get("model_version"),
            "device": body.get("device"), "worker": response.headers.get("x-ocr-worker") if response is not None else None,
            "expected_texts": expected, "actual_texts": actuals,
            "cer_mean": mean(errors), "exact_rate": mean(value == 0 for value in errors),
            "item_count": len(ocr_texts(body)), "confidence_mean": mean(scores),
            "error": error or (body.get("detail") if response is not None and response.status_code >= 400 else None),
        }
        self.results.append(result)
        return result

    async def run_ocr(self) -> None:
        for name in IMAGE_CASES:
            asset = self.assets[name]
            await self.ocr_call(
                name,
                [{"image_id": name, "url": asset["url"], "regions": []}],
                [asset["expected"]], asset["hint"], "ocr-quality",
            )
        for name in ("format-JPG", "format-webp"):
            asset = self.assets[name]
            await self.ocr_call(name, [{"image_id": name, "url": asset["url"], "regions": []}], [asset["expected"]], "zh", "ocr-format")
        roi = self.assets["roi"]
        await self.ocr_call(
            "roi-two-regions",
            [{"image_id": "roi", "url": roi["url"], "regions": [
                {"name": "top", "bbox": [0, 0, 1280, 360]},
                {"name": "bottom", "bbox": [0, 360, 1280, 720]},
            ]}], [roi["expected"]], "zh", "ocr-roi",
        )
        batch_names = list(IMAGE_CASES)
        await self.ocr_call(
            "batch-six",
            [{"image_id": name, "url": self.assets[name]["url"], "regions": []} for name in batch_names],
            [self.assets[name]["expected"] for name in batch_names], "zh", "ocr-batch",
        )
        await self.ocr_call(
            "batch-twenty",
            [{"image_id": f"copy-{index:02d}", "url": self.assets["zh-clean"]["url"], "regions": []} for index in range(20)],
            [self.assets["zh-clean"]["expected"]] * 20, "zh", "ocr-batch",
        )
        benchmark = self.assets["zh-clean"]
        for concurrency in (1, 2, 4):
            for start in range(0, 10, concurrency):
                await asyncio.gather(*(
                    self.ocr_call(
                        f"benchmark-c{concurrency}",
                        [{"image_id": f"bench-{index}", "url": benchmark["url"], "regions": []}],
                        [benchmark["expected"]], "zh", "ocr-benchmark", concurrency, index,
                    )
                    for index in range(start, min(start + concurrency, 10))
                ))
        await self.ocr_negative()

    async def ocr_negative(self) -> None:
        cases = [
            ("negative-ssrf", {"images": [{"image_id": "x", "url": "https://127.0.0.1/x.png", "regions": []}]}, {400, 403, 422}),
            ("negative-too-many", {"images": [{"image_id": str(index), "url": self.assets["zh-clean"]["url"], "regions": []} for index in range(21)]}, {422}),
        ]
        for case_id, payload, expected_statuses in cases:
            response, latency, error = await self.public.request("POST", "/v1/ocr/batch", json=payload)
            status = response.status_code if response is not None else 0
            self.results.append({"case_id": case_id, "phase": "ocr-negative", "http_status": status, "latency_seconds": round(latency, 4), "rejection_passed": status in expected_statuses, "error": error})
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.args.base_url.rstrip("/") + "/v1/ocr/batch", json={"images": [{"image_id": "x", "url": self.assets["zh-clean"]["url"], "regions": []}]})
        self.results.append({"case_id": "negative-no-auth", "phase": "ocr-negative", "http_status": response.status_code, "rejection_passed": response.status_code == 401})

    async def run_subtitle(self) -> None:
        source = Path(self.assets["subtitle-video"]["path"])
        for mode in ("fast", "balanced", "accurate"):
            output_dir = self.output / "subtitle-results" / mode
            payload = {
                "input_path": str(source.resolve()), "output_dir": str(output_dir.resolve()),
                "source_lang_hint": "zh", "mode": mode, "export_debug_video": True,
                "asr_segments": [], "config": {},
            }
            response, latency, error = await self.internal.request("POST", "/internal/admin/subtitle/detect", json=payload)
            body: dict[str, Any] = {}
            if response is not None:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
            detail: dict[str, Any] = {}
            events_path = Path(str(body.get("events_json") or ""))
            if events_path.is_file():
                detail = json.loads(events_path.read_text(encoding="utf-8"))
            events = subtitle_events(detail)
            actual_text = " ".join(str(event.get("text") or "") for event in events)
            expected_text = " ".join(item["text"] for item in self.assets["subtitle-video"]["expected_events"])
            cer = text_errors(expected_text, actual_text)[0]
            self.results.append({
                "case_id": f"subtitle-{mode}", "phase": "subtitle", "mode": mode,
                "http_status": response.status_code if response is not None else 0,
                "latency_seconds": round(latency, 4), "status": body.get("status"),
                "event_count": len(events), "expected_event_count": 3,
                "expected_text": expected_text, "actual_text": actual_text, "cer": cer,
                "metrics": body.get("metrics"), "events_json_exists": events_path.is_file(),
                "debug_video_exists": Path(str(body.get("debug_video") or "")).is_file(),
                "review_html_exists": Path(str(body.get("review_html") or "")).is_file(),
                "error": error or (body.get("detail") if response is not None and response.status_code >= 400 else None),
            })
        response, latency, error = await self.internal.request(
            "POST", "/internal/admin/subtitle/detect",
            json={"input_path": "/etc/passwd", "mode": "fast", "export_debug_video": False},
        )
        self.results.append({
            "case_id": "subtitle-path-traversal", "phase": "subtitle-negative",
            "http_status": response.status_code if response is not None else 0,
            "latency_seconds": round(latency, 4),
            "rejection_passed": response is not None and response.status_code == 422,
            "error": error,
        })

    def summary(self, resources: list[dict[str, Any]], started: str, finished: str) -> dict[str, Any]:
        ocr = [row for row in self.results if row.get("phase", "").startswith("ocr-") and row.get("phase") != "ocr-negative"]
        benchmark = [row for row in ocr if row.get("phase") == "ocr-benchmark"]
        subtitle = [row for row in self.results if row.get("phase") == "subtitle"]
        negative = [row for row in self.results if "negative" in row.get("phase", "")]
        concurrency: dict[str, Any] = {}
        for level in (1, 2, 4):
            rows = [row for row in benchmark if row.get("concurrency") == level]
            latencies = [row["latency_seconds"] for row in rows]
            concurrency[str(level)] = {
                "requests": len(rows), "success_rate": mean(row["http_status"] == 200 for row in rows),
                "p50": percentile(latencies, .5), "p95": percentile(latencies, .95), "p99": percentile(latencies, .99),
            }
        return {
            "module": "OCR与字幕", "run_id": self.args.run_id,
            "started_at": started, "finished_at": finished, "requests": len(self.results),
            "ocr_requests": len(ocr), "ocr_success_rate": mean(row["http_status"] == 200 for row in ocr),
            "ocr_exact_rate": mean((row.get("exact_rate") or 0) == 1 for row in ocr),
            "ocr_cer_mean": mean(row.get("cer_mean") or 0 for row in ocr),
            "ocr_p95": percentile([row["latency_seconds"] for row in ocr], .95),
            "subtitle_modes": len(subtitle),
            "subtitle_success_rate": mean(row["http_status"] == 200 for row in subtitle),
            "subtitle_event_count_pass_rate": mean(row.get("event_count") == 3 for row in subtitle),
            "subtitle_cer_mean": mean(row.get("cer") or 0 for row in subtitle),
            "negative_pass_rate": mean(bool(row.get("rejection_passed")) for row in negative),
            "concurrency": concurrency,
            "failures": json_safe([row for row in self.results if (row.get("phase") in {"ocr-negative", "subtitle-negative"} and not row.get("rejection_passed")) or (row.get("phase") not in {"ocr-negative", "subtitle-negative"} and row.get("http_status") != 200) or (isinstance(row.get("cer_mean"), (int, float)) and row["cer_mean"] > .10) or (isinstance(row.get("cer"), (int, float)) and row["cer"] > .10)]),
            "resources": resource_summary(resources),
        }

    def write_report(self, summary: dict[str, Any]) -> None:
        concurrency_rows = [
            f"| {level} | {item['requests']} | {item['success_rate']:.1%} | {item['p50']:.3f}s | {item['p95']:.3f}s | {item['p99']:.3f}s |"
            for level, item in summary["concurrency"].items()
        ]
        subtitle_rows = [
            f"| {row['mode']} | {row['http_status']} | {row['event_count']}/3 | {row['cer']:.4f} | {row['latency_seconds']:.2f}s | {row['debug_video_exists']} | {row['review_html_exists']} |"
            for row in self.results if row.get("phase") == "subtitle"
        ]
        report = f"""# AI Centre OCR与字幕生产测试报告

- 测试批次：`{self.args.run_id}`
- 覆盖：中英混合、数字、小字、噪声、旋转、PNG/JPG/WebP、ROI、1/6/20图批量、并发1/2/4，以及6秒三段受控字幕视频。

## 一、执行摘要

| 指标 | 结果 |
| --- | ---: |
| OCR有效请求 | {summary['ocr_requests']} |
| OCR HTTP成功率 | {summary['ocr_success_rate']:.2%} |
| OCR请求级完全一致率 | {summary['ocr_exact_rate']:.2%} |
| OCR平均CER | {summary['ocr_cer_mean']:.4f} |
| OCR P95 | {summary['ocr_p95']:.3f}s |
| 字幕三种模式成功率 | {summary['subtitle_success_rate']:.2%} |
| 字幕事件数正确率 | {summary['subtitle_event_count_pass_rate']:.2%} |
| 字幕平均CER | {summary['subtitle_cer_mean']:.4f} |
| 鉴权、SSRF、数量和路径限制通过率 | {summary['negative_pass_rate']:.2%} |

## 二、OCR并发性能

| 并发 | 请求数 | 成功率 | P50 | P95 | P99 |
| ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(concurrency_rows)}

## 三、字幕检测

| 模式 | HTTP | 事件数 | CER | 耗时 | 调试视频 | 审核HTML |
| --- | ---: | ---: | ---: | ---: | --- | --- |
{chr(10).join(subtitle_rows)}

## 四、异常与边界

- CER超过10%、HTTP异常或安全用例未按预期拒绝：{len(summary['failures'])} 条。
- OCR准确率按去标点、大小写归一化后的字符误差计算；空格和检测框顺序可能由OCR引擎调整。
- 字幕素材在0/2/4秒切换，三种模式均检查事件数、文字、调试视频和审核HTML产物。

## 五、产物

- `PROCESS.zh-CN.md`：完整测试流程。
- `results.csv/json`、`summary.json`：逐请求与聚合结果。
- `subtitle-results/`：事件JSON、QA、调试视频和审核HTML。
- `resources.csv/json`：逐秒资源采样。
"""
        (self.output / "REPORT.zh-CN.md").write_text(report, encoding="utf-8")
        process = f"""# OCR与字幕测试流程

1. 用固定字体生成中文、英文、中英混合、数字、小字、加噪和旋转图片，并转换PNG/JPG/WebP。
2. 调用 `POST /v1/ocr/batch`，分别验证单图、ROI、6图和20图批量。
3. 并发1/2/4各执行10次干净中文图片，统计P50/P95/P99。
4. 生成6秒视频：0-2秒“第一段字幕”、2-4秒“第二段字幕”、4-6秒“第三段字幕”。
5. 经本机内部接口分别运行fast/balanced/accurate，检查事件、文字、QA、调试视频和审核HTML。
6. 验证无Token、SSRF、超量图片和runtime目录之外的字幕路径均被拒绝。
7. 保存CER、置信度、Worker、模型版本、耗时和逐秒资源采样。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_ocr_subtitle_module.py \\
  --run-id {self.args.run_id} \\
  --output-dir runtime/validation/{self.args.run_id} \\
  --font runtime/validation/assets/msyh.ttc
```
"""
        (self.output / "PROCESS.zh-CN.md").write_text(process, encoding="utf-8")


async def main_async(args: argparse.Namespace) -> None:
    env = load_env(args.env_file)
    validator = Validator(args, env)
    sampler = ResourceSampler()
    sampler_task = asyncio.create_task(sampler.run())
    started = now_iso()
    try:
        await validator.prepare_assets()
        await validator.run_ocr()
        await validator.run_subtitle()
    finally:
        sampler.stop()
        await sampler_task
        await validator.close()
    finished = now_iso()
    summary = validator.summary(sampler.samples, started, finished)
    write_artifacts(args.output_dir, validator.results, sampler.samples, summary)
    validator.write_report(summary)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"ocr-subtitle-validation-{time.strftime('%Y%m%d')}")
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--internal-url", default="http://127.0.0.1:8320")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--font", type=Path, default=Path("runtime/validation/assets/msyh.ttc"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    args.output_dir = args.output_dir or Path("runtime/validation") / args.run_id
    return args


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
