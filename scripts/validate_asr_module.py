#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
from pypinyin import Style, lazy_pinyin

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
    edit_distance,
    normalize_text,
    text_errors,
    write_artifacts,
)


LANGUAGES = {
    "zh": ("中国·普通话", False), "en": ("美国·英语", True),
    "es": ("西班牙·西班牙语", True), "fr": ("法国·法语", True),
    "de": ("德国·德语", True), "it": ("意大利·意大利语", True),
    "pt": ("巴西·葡萄牙语", True), "ja": ("日本·日语", False),
    "ko": ("韩国·韩语", False), "ru": ("俄罗斯·俄语", True),
    "ar": ("沙特阿拉伯·阿拉伯语", True),
}

ZH_TEXTS = {
    "short": "欢迎使用语音识别。",
    "medium": "今天我们测试语音识别系统，确认文字准确、时间分段连续，并且没有遗漏重要内容。",
    "long": (
        "可靠的语音识别服务需要在不同音频格式、不同文本长度和不同并发条件下保持稳定。"
        "本次测试会检查识别文字是否准确，语言是否能够自动判断，分段时间轴是否连续，以及接口在并发请求下的响应速度。"
        "对于生产系统来说，单次成功并不能代表长期可靠，因此我们还会重复调用短音频，并统计中位数、九十五分位和九十九分位耗时。"
        "所有测试素材均由当前系统生成并归档，只用于验证本项目的功能，不包含真实客户的隐私内容。"
    ),
}


def transcription_text(payload: dict[str, Any]) -> str:
    direct = payload.get("text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return "".join(
        str(segment.get("text") or "")
        for segment in payload.get("segments") or []
        if isinstance(segment, dict)
    ).strip()


def segment_valid(payload: dict[str, Any]) -> bool:
    previous_end = 0.0
    for segment in payload.get("segments") or []:
        if not isinstance(segment, dict):
            return False
        try:
            start, end = float(segment["start"]), float(segment["end"])
        except (KeyError, TypeError, ValueError):
            return False
        if start < -0.01 or end < start or start + 0.15 < previous_end:
            return False
        previous_end = end
    return True


def phonetic_cer(expected: str, actual: str, language: str) -> float | None:
    if language != "zh":
        return None
    expected_tokens = lazy_pinyin(
        normalize_text(expected), style=Style.NORMAL, errors=lambda value: list(value)
    )
    actual_tokens = lazy_pinyin(
        normalize_text(actual), style=Style.NORMAL, errors=lambda value: list(value)
    )
    return edit_distance(expected_tokens, actual_tokens) / max(1, len(expected_tokens))


class Validator:
    def __init__(self, args: argparse.Namespace, env: dict[str, str]) -> None:
        self.args = args
        self.env = env
        self.output = args.output_dir
        self.assets_dir = self.output / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("SERVICE_TOKEN") or env.get("SERVICE_TOKEN", "")
        self.api = ApiClient(args.base_url, token, args.timeout)
        self.store = AssetStore(env, f"ai-centre/validation/asr/{args.run_id}")
        self.results: list[dict[str, Any]] = []
        self.assets: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.api.close()

    async def _tts_asset(self, name: str, text: str) -> Path:
        response, _, error = await self.api.request(
            "POST", "/v2/tts/speech",
            json={"text": text, "language": "zh", "quality_mode": "standard"},
        )
        if error or response is None or response.status_code != 200:
            raise RuntimeError(f"unable to create ASR fixture {name}: {error or response.status_code}")
        path = self.assets_dir / f"{name}.wav"
        path.write_bytes(response.content)
        return path

    async def prepare_assets(self) -> None:
        references_path = self.args.references
        references = json.loads(references_path.read_text(encoding="utf-8"))
        for language, metadata in references.items():
            if language not in LANGUAGES:
                continue
            self.assets[f"reference-{language}"] = {
                **metadata,
                "url": self.store.url(metadata["object_key"]),
                "expected": metadata["prompt_text"],
            }
        for length, text in ZH_TEXTS.items():
            wav = await self._tts_asset(f"zh-{length}", text)
            self.assets[f"zh-{length}-wav"] = {
                **self.store.upload(wav, wav.name, "audio/wav"), "expected": text,
            }
        source = self.assets_dir / "zh-medium.wav"
        for suffix, codec, content_type in (
            ("mp3", ["-codec:a", "libmp3lame", "-b:a", "128k"], "audio/mpeg"),
            ("M4A", ["-codec:a", "aac", "-b:a", "128k"], "audio/mp4"),
            ("MP3", ["-codec:a", "libmp3lame", "-b:a", "128k"], "audio/mpeg"),
        ):
            target = self.assets_dir / f"zh-medium.{suffix}"
            run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), *codec, str(target)])
            self.assets[f"format-{suffix}"] = {
                **self.store.upload(target, target.name, content_type),
                "expected": ZH_TEXTS["medium"],
            }
        video = self.assets_dir / "zh-medium-video.mp4"
        run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x203040:s=640x360:r=25",
            "-i", str(source), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(video),
        ])
        self.assets["format-video"] = {
            **self.store.upload(video, video.name, "video/mp4"),
            "expected": ZH_TEXTS["medium"],
        }
        image = np.full((240, 640, 3), 255, dtype=np.uint8)
        cv2.putText(image, "NOT AUDIO", (80, 140), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
        invalid = self.assets_dir / "not-audio.png"
        cv2.imwrite(str(invalid), image)
        self.assets["invalid-image"] = self.store.upload(invalid, invalid.name, "image/png")
        safe_assets = {
            key: {item_key: item_value for item_key, item_value in value.items() if item_key != "url"}
            for key, value in self.assets.items()
        }
        (self.output / "fixtures.json").write_text(
            json.dumps(safe_assets, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def call(
        self,
        case_id: str,
        asset: dict[str, Any],
        language: str | None,
        beam_size: int,
        phase: str,
        concurrency: int = 1,
        sample_index: int = 0,
    ) -> dict[str, Any]:
        payload = {"file_url": asset["url"], "language": language, "beam_size": beam_size}
        response, latency, error = await self.api.request("POST", "/v1/asr/transcriptions", json=payload)
        body: dict[str, Any] = {}
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                body = {}
        expected = str(asset.get("expected") or "")
        actual = transcription_text(body)
        normalized_language = language if language not in {None, "auto"} else str(body.get("language") or "")
        spaced = normalized_language in {"en", "es", "fr", "de", "it", "pt", "ru", "ar"}
        cer, wer = text_errors(expected, actual, spaced) if expected else (None, None)
        p_cer = phonetic_cer(expected, actual, normalized_language) if expected else None
        result = {
            "case_id": case_id, "phase": phase, "sample_index": sample_index,
            "concurrency": concurrency, "requested_language": language or "omitted",
            "detected_language": body.get("language"), "beam_size": beam_size,
            "http_status": response.status_code if response is not None else 0,
            "latency_seconds": round(latency, 4), "expected_text": expected,
            "actual_text": actual, "cer": cer, "wer": wer,
            "content_exact": cer == 0 if cer is not None else None,
            "phonetic_cer": p_cer,
            "pronunciation_exact": cer == 0 or p_cer == 0 if cer is not None else None,
            "segments": len(body.get("segments") or []),
            "segment_timeline_valid": segment_valid(body),
            "audio_duration": body.get("duration"),
            "error": error or (body.get("detail") if response is not None and response.status_code >= 400 else None),
        }
        self.results.append(result)
        return result

    async def run_matrix(self) -> None:
        for language in LANGUAGES:
            asset = self.assets[f"reference-{language}"]
            await self.call(f"language-{language}-auto", asset, "auto", 5, "language")
            await self.call(f"language-{language}-explicit", asset, language, 5, "language")
        for length in ZH_TEXTS:
            asset = self.assets[f"zh-{length}-wav"]
            for beam in (1, 5, 10):
                await self.call(f"length-{length}-beam-{beam}", asset, "auto", beam, "length-beam")
        for name in ("format-mp3", "format-M4A", "format-MP3", "format-video"):
            await self.call(name, self.assets[name], "auto", 5, "format")
        benchmark = self.assets["zh-short-wav"]
        for concurrency in (1, 2, 4):
            for start in range(0, 10, concurrency):
                await asyncio.gather(*(
                    self.call(
                        f"benchmark-c{concurrency}", benchmark, "auto", 5,
                        "benchmark", concurrency, index,
                    )
                    for index in range(start, min(start + concurrency, 10))
                ))
        await self.negative_tests()

    async def negative_tests(self) -> None:
        cases = [
            ("negative-ssrf", {"file_url": "https://127.0.0.1/private.wav", "language": "auto", "beam_size": 5}, {400, 403, 422}),
            ("negative-content-type", {"file_url": self.assets["invalid-image"]["url"], "language": "auto", "beam_size": 5}, {415}),
            ("negative-beam", {"file_url": self.assets["zh-short-wav"]["url"], "language": "auto", "beam_size": 11}, {422}),
        ]
        for case_id, payload, expected_statuses in cases:
            response, latency, error = await self.api.request("POST", "/v1/asr/transcriptions", json=payload)
            status = response.status_code if response is not None else 0
            self.results.append({
                "case_id": case_id, "phase": "negative", "http_status": status,
                "latency_seconds": round(latency, 4), "expected_rejection": True,
                "rejection_passed": status in expected_statuses, "error": error,
            })
        async with httpx.AsyncClient(timeout=20) as client:
            started = time.perf_counter()
            response = await client.post(
                self.args.base_url.rstrip("/") + "/v1/asr/transcriptions",
                json={"file_url": self.assets["zh-short-wav"]["url"], "language": "auto", "beam_size": 5},
            )
            self.results.append({
                "case_id": "negative-no-auth", "phase": "negative",
                "http_status": response.status_code,
                "latency_seconds": round(time.perf_counter() - started, 4),
                "expected_rejection": True, "rejection_passed": response.status_code == 401,
            })

    def summary(self, resources: list[dict[str, Any]], started_at: str, finished_at: str) -> dict[str, Any]:
        positive = [row for row in self.results if row.get("phase") != "negative"]
        scored = [row for row in positive if isinstance(row.get("cer"), (int, float))]
        benchmarks = [row for row in positive if row.get("phase") == "benchmark"]
        by_concurrency: dict[str, Any] = {}
        for concurrency in (1, 2, 4):
            rows = [row for row in benchmarks if row.get("concurrency") == concurrency]
            latencies = [row["latency_seconds"] for row in rows]
            by_concurrency[str(concurrency)] = {
                "requests": len(rows), "success_rate": mean(row["http_status"] == 200 for row in rows),
                "p50": percentile(latencies, 0.50), "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
            }
        negatives = [row for row in self.results if row.get("phase") == "negative"]
        return {
            "module": "ASR 语音识别", "run_id": self.args.run_id,
            "started_at": started_at, "finished_at": finished_at,
            "requests": len(self.results), "positive_requests": len(positive),
            "http_success_rate": mean(row["http_status"] == 200 for row in positive),
            "exact_rate": mean(bool(row.get("content_exact")) for row in scored),
            "pronunciation_exact_rate": mean(bool(row.get("pronunciation_exact")) for row in scored),
            "cer_mean": mean(row["cer"] for row in scored),
            "timeline_valid_rate": mean(bool(row.get("segment_timeline_valid")) for row in positive),
            "p50_latency": percentile([row["latency_seconds"] for row in positive], 0.50),
            "p95_latency": percentile([row["latency_seconds"] for row in positive], 0.95),
            "p99_latency": percentile([row["latency_seconds"] for row in positive], 0.99),
            "negative_security_pass_rate": mean(bool(row.get("rejection_passed")) for row in negatives),
            "concurrency": by_concurrency,
            "failures": json_safe([row for row in self.results if row.get("http_status") not in {200, 401, 415, 422} or (row.get("phase") == "negative" and not row.get("rejection_passed")) or (isinstance(row.get("cer"), (int, float)) and row["cer"] > 0.10 and (row.get("phonetic_cer") is None or row["phonetic_cer"] > 0.10))]),
            "resources": resource_summary(resources),
        }

    def write_report(self, summary: dict[str, Any]) -> None:
        language_rows = []
        for language, (name, _) in LANGUAGES.items():
            rows = [row for row in self.results if row.get("case_id", "").startswith(f"language-{language}-")]
            language_rows.append(
                f"| {name}（{language}） | {len(rows)} | {sum(row.get('http_status') == 200 for row in rows) / max(1, len(rows)):.1%} | "
                f"{(mean(row.get('cer', 1) for row in rows) or 0):.4f} | {(percentile([row['latency_seconds'] for row in rows], .95) or 0):.2f}s |"
            )
        concurrency_rows = []
        for concurrency, item in summary["concurrency"].items():
            concurrency_rows.append(
                f"| {concurrency} | {item['requests']} | {item['success_rate']:.1%} | {item['p50']:.2f}s | {item['p95']:.2f}s | {item['p99']:.2f}s |"
            )
        failures = summary["failures"]
        report = f"""# AI Centre ASR 语音识别生产测试报告

- 测试批次：`{self.args.run_id}`
- 开始时间（UTC）：`{summary['started_at']}`
- 完成时间（UTC）：`{summary['finished_at']}`
- 覆盖：11种语言、auto/显式语言、短中长文本、WAV/MP3/M4A/MP4、大写扩展名、Beam 1/5/10、并发1/2/4和安全拒绝。

## 一、执行摘要

| 指标 | 结果 |
| --- | ---: |
| 有效业务请求 | {summary['positive_requests']} |
| HTTP成功率 | {summary['http_success_rate']:.2%} |
| ASR逐字完全一致率 | {summary['exact_rate']:.2%} |
| ASR发音/简繁等价一致率 | {summary['pronunciation_exact_rate']:.2%} |
| 平均CER | {summary['cer_mean']:.4f} |
| 分段时间轴有效率 | {summary['timeline_valid_rate']:.2%} |
| P50 / P95 / P99 | {summary['p50_latency']:.2f}s / {summary['p95_latency']:.2f}s / {summary['p99_latency']:.2f}s |
| 鉴权、SSRF、格式和参数拒绝通过率 | {summary['negative_security_pass_rate']:.2%} |

## 二、各语言识别结果

| 国家/语言 | 请求数 | HTTP成功率 | 平均CER | P95 |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(language_rows)}

## 三、并发性能

| 并发 | 请求数 | 成功率 | P50 | P95 | P99 |
| ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(concurrency_rows)}

## 四、异常与边界

- CER超过10%、HTTP异常或安全用例未按预期拒绝的记录：{len(failures)} 条。
- 失败明细保存在 `summary.json` 和 `results.json`，完整识别文本仅为本次合成测试文案。
- ASR准确率会同时包含生成测试音频自身的发音误差；11语种参考音频来自上一轮已归档的VoxCPM2生产样本。

## 五、产物

- `PROCESS.zh-CN.md`：可复现测试流程。
- `results.csv/json`：逐请求结果、CER/WER、语言和分段校验。
- `resources.csv/json`：逐秒GPU、CPU、内存、功耗和温度采样。
- `fixtures.json`：测试素材对象键与元数据，不包含Token或签名URL。
"""
        (self.output / "REPORT.zh-CN.md").write_text(report, encoding="utf-8")
        process = f"""# ASR 语音识别测试流程

1. 确认 `/health` 中 ASR 为 `ok`，且 `ai-centre-asr-gpu0.service` 正常运行。
2. 使用固定文案生成短、中、长中文WAV；转换为MP3、M4A、大写`.MP3`并封装一个MP4。
3. 复用11语种已归档参考音频，分别以 `language=auto` 和显式语言调用 `POST /v1/asr/transcriptions`。
4. 对中文短中长素材执行Beam 1/5/10测试；对格式样本验证文件头识别。
5. 并发1/2/4各执行10次短音频，统计P50/P95/P99。
6. 验证无Token、localhost/内网URL、非音视频内容和越界Beam均被拒绝。
7. 对返回正文计算CER/WER，检查segment起止时间有序且不倒退。
8. 保存逐请求结果和逐秒资源采样，不保存Service Token或OSS签名参数。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_asr_module.py \\
  --run-id {self.args.run_id} \\
  --output-dir runtime/validation/{self.args.run_id} \\
  --references runtime/validation/tts-retest-20260817/references.json
```
"""
        (self.output / "PROCESS.zh-CN.md").write_text(process, encoding="utf-8")


async def main_async(args: argparse.Namespace) -> None:
    env = load_env(args.env_file)
    validator = Validator(args, env)
    sampler = ResourceSampler()
    sampler_task = asyncio.create_task(sampler.run())
    started_at = now_iso()
    try:
        await validator.prepare_assets()
        await validator.run_matrix()
    finally:
        sampler.stop()
        await sampler_task
        await validator.close()
    finished_at = now_iso()
    summary = validator.summary(sampler.samples, started_at, finished_at)
    write_artifacts(args.output_dir, validator.results, sampler.samples, summary)
    validator.write_report(summary)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"asr-validation-{time.strftime('%Y%m%d')}")
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--references", type=Path, default=Path("runtime/validation/tts-retest-20260817/references.json"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    args.output_dir = args.output_dir or Path("runtime/validation") / args.run_id
    return args


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
