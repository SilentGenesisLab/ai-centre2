from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from control_plane.config import get_settings
from control_plane.h3_store import H3Store


TERMINAL = {"succeeded", "failed", "cancelled"}
RESOLUTIONS = ("480p", "720p", "1080p")
DURATIONS = (5, 10, 15)
INPUT_MODES = ("text", "video")
PRIORITY = 1
DEFAULT_MAX_IN_FLIGHT = 3
DEFAULT_BASE_URL = "https://aicentre2.sligenai.cn:8443"
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

CREATIVES: dict[str, dict[str, Any]] = {
    "smart_watch": {
        "label": "高端智能手表",
        "reference_video_url": (
            "https://oss-imgai.sligenai.cn/ai-video-kernel/20260903/"
            "15846e27c74a43a3b27d56a90994a814.mp4"
        ),
        "prompts": {
            5: (
                "高端智能手表TVC广告，深黑镜面展台，银色圆形表壳与蓝色表盘保持稳定一致。"
                "开场微距扫过金属表冠和表盘纹理，冷蓝轮廓光掠过边缘，镜头顺滑环绕后停在英雄产品镜头。"
                "真实商业摄影，材质精确，运动连续，背景干净，不生成文字、Logo、水印或多余产品。"
            ),
            10: (
                "高端智能手表TVC广告，深黑镜面展台，银色圆形表壳与蓝色表盘全程保持稳定一致。"
                "先以微距镜头扫过金属表冠和玻璃反光，再缓慢环绕展示纤薄轮廓，蓝色能量光带沿表壳流动，"
                "最后停在正面英雄镜头。冷蓝电影级灯光，真实高级材质，节奏清晰，镜头平稳，"
                "不生成文字、Logo、水印或多余产品。"
            ),
            15: (
                "高端智能手表完整TVC广告，深黑镜面展台，银色圆形表壳与蓝色表盘全程保持同一产品身份。"
                "第一阶段微距展示表冠、拉丝金属和玻璃纹理；第二阶段镜头平滑环绕，蓝色轮廓光与轻微粒子强调科技感；"
                "第三阶段表盘朝向镜头形成干净的英雄定格。真实商业摄影，电影级冷蓝灯光，高对比但保留暗部细节，"
                "动作和反射连续，不变形，不复制产品，不生成文字、Logo、水印。"
            ),
        },
    },
    "blue_serum": {
        "label": "浅蓝科技护肤精华",
        "reference_video_url": (
            "https://oss-imgai.sligenai.cn/ai-video-kernel/20260901/"
            "ca4c8b26e6504bdaa5582d95d6d2c103.mp4"
        ),
        "prompts": {
            5: (
                "浅蓝科技风护肤精华TVC广告，透明磨砂精华瓶立在浅蓝水面，产品造型始终稳定。"
                "微小水珠悬浮，柔和波纹向外扩散，镜头缓慢推进到英雄产品镜头。"
                "清透高级、真实商业摄影、柔和体积光，不生成文字、Logo、水印、手或多余瓶子。"
            ),
            10: (
                "浅蓝科技风护肤精华TVC广告，透明磨砂精华瓶立在浅蓝水面，产品比例与瓶盖全程稳定一致。"
                "镜头从水面倒影平滑抬升，微小水珠和半透明分子光点环绕产品，柔和波纹扩散，"
                "随后缓慢推进形成正面英雄镜头。清透高级美妆摄影，浅蓝白配色，柔和体积光，"
                "不生成文字、Logo、水印、手或多余瓶子。"
            ),
            15: (
                "浅蓝科技风护肤精华完整TVC广告，透明磨砂精华瓶立在浅蓝水面，产品轮廓、瓶盖与材质全程一致。"
                "第一阶段从水面倒影和瓶身水珠微距开始；第二阶段半透明分子光点沿瓶身上升，水面形成细腻波纹，"
                "镜头轻柔环绕展示玻璃质感；第三阶段镜头缓慢推进到居中的英雄产品定格。"
                "清透高级美妆商业摄影，浅蓝白科技配色，柔和体积光，画面连续稳定，"
                "不变形，不复制产品，不生成文字、Logo、水印、手或多余瓶子。"
            ),
        },
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(result, 3)


def metric_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3) if values else None,
        "min": round(min(values), 3) if values else None,
        "max": round(max(values), 3) if values else None,
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
    }


def build_cases(run_id: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    # Interleave costly and cheap cases so one resolution does not inherit all warm/cold effects.
    order = (
        ("480p", 5), ("720p", 10), ("1080p", 15),
        ("1080p", 5), ("480p", 10), ("720p", 15),
        ("720p", 5), ("1080p", 10), ("480p", 15),
    )
    for resolution, duration in order:
        for creative_name, creative in CREATIVES.items():
            for input_mode in INPUT_MODES:
                case_id = f"{creative_name}-{resolution}-{duration}s-{input_mode}"
                prompt = str(creative["prompts"][duration])
                if input_mode == "video":
                    prompt = (
                        "以<Video 1>作为产品身份、材质、构图和镜头节奏参考，保持参考主体的一致性；"
                        + prompt
                    )
                seed = int.from_bytes(case_id.encode("utf-8"), "little") % (2**63 - 1)
                payload: dict[str, Any] = {
                    "reference_video_urls": (
                        [str(creative["reference_video_url"])] if input_mode == "video" else []
                    ),
                    "segment_mode": "single",
                    "prompts": [prompt],
                    "duration_seconds": duration,
                    "resolution": resolution,
                    "aspect_ratio": "16:9",
                    "priority": PRIORITY,
                    "seed": seed,
                    "external_ref": f"{run_id}-{case_id}",
                    "metadata": {
                        "purpose": "tvc-benchmark",
                        "run_id": run_id,
                        "creative": creative_name,
                        "input_mode": input_mode,
                    },
                }
                cases.append({
                    "case_id": case_id,
                    "creative": creative_name,
                    "creative_label": creative["label"],
                    "resolution": resolution,
                    "duration_seconds": duration,
                    "input_mode": input_mode,
                    "priority": PRIORITY,
                    "prompt": prompt,
                    "reference_video_url": (
                        str(creative["reference_video_url"]) if input_mode == "video" else None
                    ),
                    "payload": payload,
                    "status": "pending",
                    "created_at": utc_now(),
                })
    return cases


def response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"detail": value}
    except ValueError:
        return {"detail": response.text[:500]}


def worker_sample(client: httpx.Client) -> dict[str, Any]:
    response = client.get("/v1/video-generations/minimax-h3/workers/status")
    return {
        "sampled_at": utc_now(),
        "http_status": response.status_code,
        **(response_json(response) if response.status_code == 200 else {}),
    }


def submit_case(client: httpx.Client, case: dict[str, Any]) -> None:
    started = time.perf_counter()
    response = client.post("/v1/video-generations/minimax-h3/jobs", json=case["payload"])
    body = response_json(response)
    case["submit_http"] = response.status_code
    case["submit_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    case["submitted_at"] = utc_now()
    if response.status_code == 202 and body.get("job_id"):
        case["job_id"] = str(body["job_id"])
        case["status"] = "queued"
        case["stage"] = "queued"
        return
    case["status"] = "submit_failed"
    case["error"] = str(body.get("detail") or f"HTTP {response.status_code}")[:1000]


def poll_case(client: httpx.Client, case: dict[str, Any]) -> None:
    response = client.get(f"/v1/video-generations/minimax-h3/jobs/{case['job_id']}")
    case["last_polled_at"] = utc_now()
    if response.status_code != 200:
        case["poll_error"] = f"HTTP {response.status_code}: {response.text[:300]}"
        return
    job = response_json(response)
    case["status"] = str(job.get("status") or "unknown")
    case["stage"] = job.get("stage")
    case["progress"] = job.get("progress")
    case["attempt_count"] = job.get("attempt_count")
    if case["status"] in TERMINAL:
        case["finished_poll_at"] = utc_now()
        case["job"] = job
        case["result_url"] = job.get("result_url")
        case["error"] = job.get("error")
        case["timing"] = job.get("timing") or {}
        case.pop("payload", None)


def run_command(command: list[str], timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def download_media(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=httpx.Timeout(900, connect=30), trust_env=False
    ) as response:
        response.raise_for_status()
        with temporary.open("wb") as stream:
            for chunk in response.iter_bytes(1024 * 1024):
                stream.write(chunk)
    os.replace(temporary, target)


def probe_media(source: str) -> dict[str, Any]:
    decode = run_command([FFMPEG_EXE, "-hide_banner", "-i", source, "-f", "null", "-"], timeout=600)
    stderr = decode.stderr or ""
    result: dict[str, Any] = {
        "probe_backend": "imageio_ffmpeg",
        "complete_decode": decode.returncode == 0,
        "decode_error": stderr[-1000:] if decode.returncode else None,
        "format": {},
        "streams": [],
    }
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if duration_match:
        duration = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )
        result["format"]["duration"] = f"{duration:.6f}"
    video_match = re.search(
        r"Video:\s*([^,]+).*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?)\s+fps",
        stderr,
    )
    if video_match:
        result["streams"].append({
            "index": 0,
            "codec_type": "video",
            "codec_name": video_match.group(1).strip(),
            "width": int(video_match.group(2)),
            "height": int(video_match.group(3)),
            "avg_frame_rate": video_match.group(4),
        })
    audio_match = re.search(r"Audio:\s*([^,]+).*?(\d+)\s+Hz.*?\b(mono|stereo|\d+ channels)\b", stderr)
    if audio_match:
        channel_label = audio_match.group(3)
        channels = 1 if channel_label == "mono" else 2 if channel_label == "stereo" else int(channel_label.split()[0])
        result["streams"].append({
            "index": len(result["streams"]),
            "codec_type": "audio",
            "codec_name": audio_match.group(1).strip(),
            "sample_rate": audio_match.group(2),
            "channels": channels,
        })
    try:
        source_path = Path(source)
        if source_path.is_file():
            result["format"]["size"] = str(source_path.stat().st_size)
    except Exception:
        pass
    if decode.returncode == 0:
        detect = run_command([
            FFMPEG_EXE, "-hide_banner", "-nostats", "-i", source,
            "-vf", "blackdetect=d=0.20:pix_th=0.10,freezedetect=n=-50dB:d=0.50",
            "-an", "-f", "null", "-",
        ], timeout=600)
        black = re.findall(r"black_start:([0-9.]+).*?black_end:([0-9.]+).*?black_duration:([0-9.]+)", detect.stderr)
        freeze_starts = re.findall(r"freeze_start: ([0-9.]+)", detect.stderr)
        freeze_durations = re.findall(r"freeze_duration: ([0-9.]+)", detect.stderr)
        result["black_segments"] = [
            {"start": float(start), "end": float(end), "duration": float(duration)}
            for start, end, duration in black
        ]
        result["freeze_starts"] = [float(value) for value in freeze_starts]
        result["freeze_durations"] = [float(value) for value in freeze_durations]
        result["detect_exit_code"] = detect.returncode
    return result


def extract_review_frames(source: str, target_dir: Path, duration: float) -> list[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    for label, fraction in (("start", 0.1), ("middle", 0.5), ("end", 0.9)):
        target = target_dir / f"{label}.jpg"
        completed = run_command([
            FFMPEG_EXE, "-v", "error", "-y", "-ss", f"{max(0.0, duration * fraction):.3f}",
            "-i", source, "-frames:v", "1", "-q:v", "2", str(target),
        ], timeout=180)
        if completed.returncode == 0 and target.is_file():
            outputs.append(target.as_posix())
    return outputs


def write_review_html(path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    cards: list[str] = []
    for case in cases:
        media = case.get("media") or {}
        timing = case.get("timing") or {}
        thumbnails = case.get("review_frames") or []
        thumb_html = "".join(
            f'<img src="{html.escape(str(Path(value).relative_to(path.parent).as_posix()))}" loading="lazy" alt="review frame">'
            for value in thumbnails if Path(value).is_file()
        )
        result_url = html.escape(str(case.get("result_url") or ""), quote=True)
        cards.append(f"""
<article class="card" data-resolution="{case['resolution']}" data-duration="{case['duration_seconds']}" data-mode="{case['input_mode']}" data-status="{case.get('status')}">
  <div class="heading"><h2>{html.escape(case['creative_label'])}</h2><span>{html.escape(case['case_id'])}</span></div>
  <div class="tags"><b>{case['resolution']}</b><b>{case['duration_seconds']}秒</b><b>{'参考视频' if case['input_mode']=='video' else '纯文本'}</b><b>{case.get('status')}</b></div>
  <div class="frames">{thumb_html or '<div class="empty">暂无审核帧</div>'}</div>
  <p>有效处理：{timing.get('effective_processing_seconds')}s；排队：{timing.get('queue_wait_seconds')}s；端到端：{timing.get('end_to_end_seconds')}s</p>
  <p>完整解码：{media.get('complete_decode')}；黑帧段：{len(media.get('black_segments') or [])}；冻结段：{len(media.get('freeze_starts') or [])}</p>
  <details><summary>查看完整 Prompt</summary><pre>{html.escape(str(case.get('prompt') or ''))}</pre></details>
  {f'<a href="{result_url}" target="_blank" rel="noreferrer">播放/下载 OSS 成片</a>' if result_url else ''}
</article>""")
    overall = summary.get("overall") or {}
    metrics = overall.get("effective_processing_seconds") or {}
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MiniMax H3 TVC 测评审核页</title>
<style>
:root{{--bg:#eef7ff;--panel:#fff;--text:#13263a;--muted:#60758a;--accent:#1677ff;--border:#cfe4f7}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#eaf6ff,#f8fbff);color:var(--text);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}}
header{{position:sticky;top:0;z-index:2;padding:22px 4vw;background:#f8fbfff0;backdrop-filter:blur(14px);border-bottom:1px solid var(--border)}}
h1{{margin:0 0 6px;font-size:25px}}.summary{{color:var(--muted)}}.filters{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}
select{{padding:8px 12px;border:1px solid var(--border);border-radius:9px;background:white;color:var(--text)}}main{{padding:24px 4vw;display:grid;grid-template-columns:repeat(auto-fill,minmax(390px,1fr));gap:18px}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:16px;box-shadow:0 10px 30px #3d8cc414}}.heading{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}h2{{margin:0;font-size:18px}}.heading span{{color:var(--muted);font-size:11px;word-break:break-all}}
.tags{{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}}.tags b{{padding:3px 8px;background:#e7f3ff;color:#0866c6;border-radius:99px;font-size:12px}}.frames{{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;background:#dbeaf6;min-height:120px}}.frames img{{width:100%;height:150px;object-fit:cover}}pre{{white-space:pre-wrap;color:var(--muted)}}a{{color:var(--accent);font-weight:650;text-decoration:none}}.empty{{padding:36px;color:var(--muted)}}
</style></head><body>
<header><h1>MiniMax H3 TVC 全矩阵审核页</h1><div class="summary">共 {summary.get('planned_jobs')} 条，成功 {summary.get('succeeded_jobs')} 条；有效处理均值 {metrics.get('mean')} 秒，P95 {metrics.get('p95')} 秒。</div>
<div class="filters"><select id="resolution"><option value="">全部分辨率</option><option>480p</option><option>720p</option><option>1080p</option></select><select id="duration"><option value="">全部时长</option><option value="5">5秒</option><option value="10">10秒</option><option value="15">15秒</option></select><select id="mode"><option value="">全部输入</option><option value="text">纯文本</option><option value="video">参考视频</option></select></div></header>
<main>{''.join(cards)}</main>
<script>for(const id of ['resolution','duration','mode'])document.getElementById(id).addEventListener('change',()=>{{for(const card of document.querySelectorAll('.card'))card.hidden=!!([...document.querySelectorAll('select')].find(s=>s.value&&card.dataset[s.id]!==s.value))}})</script>
</body></html>"""
    path.write_text(document, encoding="utf-8")


def write_markdown_report(path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# MiniMax H3 TVC 广告全矩阵测评报告",
        "",
        f"- 测试时间：{summary.get('started_at')} 至 {summary.get('generated_at')}",
        f"- 正式样本：{summary.get('planned_jobs')} 条（2类创意 × 3分辨率 × 3时长 × 2输入模式）",
        f"- 成功：{summary.get('succeeded_jobs')}/{summary.get('terminal_jobs')}，成功率：{summary.get('success_rate')}",
        f"- 完整解码通过率：{summary.get('technical_decode_pass_rate')}",
        "- 所有任务优先级为1；运行时全部H3活跃任务总数限制为3。",
        "",
        "## 耗时总览",
        "",
        "主耗时口径为最终成功 Attempt 的有效处理时间，不包含排队等待；端到端耗时单独保留。",
        "",
        "| 维度 | 分组 | 数量 | 有效均值(s) | P50(s) | P95(s) | 端到端均值(s) |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for dimension, groups in (
        ("分辨率", summary.get("by_resolution") or {}),
        ("时长", summary.get("by_duration") or {}),
        ("输入", summary.get("by_input_mode") or {}),
        ("创意", summary.get("by_creative") or {}),
    ):
        for name, group in groups.items():
            effective = group.get("effective_processing_seconds") or {}
            end_to_end = group.get("end_to_end_seconds") or {}
            lines.append(
                f"| {dimension} | {name} | {group.get('jobs')} | {effective.get('mean')} | "
                f"{effective.get('p50')} | {effective.get('p95')} | {end_to_end.get('mean')} |"
            )
    lines.extend([
        "",
        "## 逐条结果",
        "",
        "| 案例 | 输入 | 分辨率 | 目标时长 | 状态 | 有效处理(s) | 端到端(s) | 技术检测 | 成片 |",
        "|---|---|---|---:|---|---:|---:|---|---|",
    ])
    for case in cases:
        timing = case.get("timing") or {}
        media = case.get("media") or {}
        technical = (
            f"解码={media.get('complete_decode')},黑段={len(media.get('black_segments') or [])},"
            f"冻结={len(media.get('freeze_starts') or [])}"
        )
        link = f"[OSS]({case['result_url']})" if case.get("result_url") else "-"
        lines.append(
            f"| {case['creative_label']} | {case['input_mode']} | {case['resolution']} | "
            f"{case['duration_seconds']} | {case.get('status')} | "
            f"{timing.get('effective_processing_seconds')} | {timing.get('end_to_end_seconds')} | "
            f"{technical} | {link} |"
        )
    lines.extend([
        "",
        "## 效果审核说明",
        "",
        "HTML审核页为每条成片抽取10%、50%、90%三帧并提供OSS播放链接。重点人工检查广告主题符合度、产品身份稳定、参考视频镜头继承、商品形变、乱码/伪Logo、镜头稳定、动作自然以及直接剪辑可用性。最终主观结论将在人工审核后补入本报告。",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path: Path, cases: list[dict[str, Any]]) -> None:
    columns = [
        "case_id", "creative", "resolution", "duration_seconds", "input_mode", "priority",
        "job_id", "status", "stage", "submit_http", "submit_latency_ms", "attempt_count",
        "queue_wait_seconds", "effective_processing_seconds", "render_and_upload_seconds",
        "end_to_end_seconds", "result_url", "error", "actual_duration_seconds", "width", "height",
        "fps", "video_codec", "audio_codec", "complete_decode", "black_segment_count",
        "freeze_segment_count",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for case in cases:
            timing = case.get("timing") or {}
            media = case.get("media") or {}
            streams = media.get("streams") or []
            video = next((item for item in streams if item.get("codec_type") == "video"), {})
            audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
            fmt = media.get("format") or {}
            fps = video.get("avg_frame_rate") or video.get("r_frame_rate")
            writer.writerow({
                **{key: case.get(key) for key in columns},
                "queue_wait_seconds": timing.get("queue_wait_seconds"),
                "effective_processing_seconds": timing.get("effective_processing_seconds"),
                "render_and_upload_seconds": timing.get("render_and_upload_seconds"),
                "end_to_end_seconds": timing.get("end_to_end_seconds"),
                "actual_duration_seconds": fmt.get("duration"),
                "width": video.get("width"), "height": video.get("height"), "fps": fps,
                "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name"),
                "complete_decode": media.get("complete_decode"),
                "black_segment_count": len(media.get("black_segments") or []),
                "freeze_segment_count": len(media.get("freeze_starts") or []),
            })


def build_summary(cases: list[dict[str, Any]], started_at: str) -> dict[str, Any]:
    terminal = [case for case in cases if case.get("status") in TERMINAL or case.get("status") == "submit_failed"]
    succeeded = [case for case in cases if case.get("status") == "succeeded"]

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        effective = [
            float((case.get("timing") or {}).get("effective_processing_seconds"))
            for case in items if (case.get("timing") or {}).get("effective_processing_seconds") is not None
        ]
        end_to_end = [
            float((case.get("timing") or {}).get("end_to_end_seconds"))
            for case in items if (case.get("timing") or {}).get("end_to_end_seconds") is not None
        ]
        return {
            "jobs": len(items),
            "succeeded": sum(case.get("status") == "succeeded" for case in items),
            "effective_processing_seconds": metric_summary(effective),
            "end_to_end_seconds": metric_summary(end_to_end),
        }

    return {
        "started_at": started_at,
        "generated_at": utc_now(),
        "planned_jobs": len(cases),
        "terminal_jobs": len(terminal),
        "succeeded_jobs": len(succeeded),
        "success_rate": round(len(succeeded) / len(terminal), 6) if terminal else None,
        "technical_decode_pass_rate": (
            round(sum(bool((case.get("media") or {}).get("complete_decode")) for case in succeeded) / len(succeeded), 6)
            if succeeded else None
        ),
        "overall": summarize(succeeded),
        "by_resolution": {
            resolution: summarize([case for case in succeeded if case["resolution"] == resolution])
            for resolution in RESOLUTIONS
        },
        "by_duration": {
            str(duration): summarize([case for case in succeeded if case["duration_seconds"] == duration])
            for duration in DURATIONS
        },
        "by_input_mode": {
            mode: summarize([case for case in succeeded if case["input_mode"] == mode])
            for mode in INPUT_MODES
        },
        "by_creative": {
            name: summarize([case for case in succeeded if case["creative"] == name])
            for name in CREATIVES
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the MiniMax H3 TVC benchmark matrix")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--run-id", default="h3-tvc-benchmark-20260908")
    parser.add_argument("--run-dir", type=Path, default=Path("runtime_validation/h3-tvc-benchmark-20260908"))
    parser.add_argument("--max-in-flight", type=int, default=DEFAULT_MAX_IN_FLIGHT)
    parser.add_argument(
        "--max-global-active",
        type=int,
        default=3,
        help="Do not submit when all H3 jobs together already reach this cap",
    )
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--deadline-hours", type=float, default=24.0)
    parser.add_argument("--skip-media-checks", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_in_flight <= 3:
        raise ValueError("max-in-flight must be between 1 and 3 for this production benchmark")
    if args.max_global_active < 1:
        raise ValueError("max-global-active must be at least 1")

    settings = get_settings()
    store = H3Store(settings.h3_db_path)
    token = settings.service_token
    if not token:
        raise RuntimeError("SERVICE_TOKEN is not configured")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    samples_path = run_dir / "worker-samples.jsonl"
    started_at = utc_now()
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("run_id") != args.run_id:
            raise RuntimeError("run directory belongs to a different run id")
        cases = state["cases"]
        started_at = state["started_at"]
        blueprints = {case["case_id"]: case for case in build_cases(args.run_id)}
        for case in cases:
            blueprint = blueprints[case["case_id"]]
            case.setdefault("prompt", blueprint["prompt"])
            case.setdefault("reference_video_url", blueprint["reference_video_url"])
    else:
        cases = build_cases(args.run_id)
        state = {
            "run_id": args.run_id,
            "started_at": started_at,
            "base_url": args.base_url,
            "max_in_flight": args.max_in_flight,
            "priority": PRIORITY,
            "cases": cases,
            "status": "running",
        }
        atomic_json(state_path, state)

    deadline = time.monotonic() + args.deadline_hours * 3600
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(60.0, connect=20.0)
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=timeout, trust_env=False) as client:
        while time.monotonic() < deadline:
            for case in cases:
                if case.get("job_id") and case.get("status") not in TERMINAL:
                    poll_case(client, case)

            active = [
                case for case in cases
                if case.get("job_id") and case.get("status") not in TERMINAL
            ]
            global_active = [
                job for job in store.list_jobs(limit=500)["items"]
                if job.get("status") not in TERMINAL
            ]
            slots = max(
                0,
                min(
                    args.max_in_flight - len(active),
                    args.max_global_active - len(global_active),
                ),
            )
            for case in [item for item in cases if item.get("status") == "pending"][:slots]:
                submit_case(client, case)
                if case.get("job_id"):
                    active.append(case)

            sample = worker_sample(client)
            with samples_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
            completed = sum(case.get("status") in TERMINAL for case in cases)
            failed_submit = sum(case.get("status") == "submit_failed" for case in cases)
            print(
                f"PROGRESS {completed + failed_submit}/{len(cases)} "
                f"active={len(active)} global_active={len(global_active)} "
                f"online={sample.get('online')} busy={sample.get('busy')} "
                f"idle={sample.get('idle')}",
                flush=True,
            )
            state["updated_at"] = utc_now()
            state["status"] = "running"
            atomic_json(state_path, state)
            if completed + failed_submit == len(cases):
                break
            time.sleep(args.poll_seconds)
        else:
            state["status"] = "deadline_reached"
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            raise TimeoutError("benchmark deadline reached; active jobs were left running for safe resume")

    if not args.skip_media_checks:
        for index, case in enumerate(cases, start=1):
            if case.get("status") != "succeeded" or not case.get("result_url") or case.get("media"):
                continue
            print(f"MEDIA_CHECK {index}/{len(cases)} {case['case_id']}", flush=True)
            temporary_video = run_dir / ".media-check" / f"{case['case_id']}.mp4"
            if not temporary_video.is_file():
                download_media(str(case["result_url"]), temporary_video)
            case["media"] = probe_media(str(temporary_video))
            duration = float(((case["media"].get("format") or {}).get("duration") or case["duration_seconds"]))
            case["review_frames"] = extract_review_frames(
                str(temporary_video), run_dir / "review-frames" / case["case_id"], duration
            )
            temporary_video.unlink(missing_ok=True)
            atomic_json(state_path, state)

    state["status"] = "completed"
    state["finished_at"] = utc_now()
    state["summary"] = build_summary(cases, started_at)
    atomic_json(state_path, state)
    atomic_json(run_dir / "summary.json", state["summary"])
    atomic_json(run_dir / "results.json", cases)
    write_csv(run_dir / "results.csv", cases)
    write_review_html(run_dir / "review.html", cases, state["summary"])
    write_markdown_report(
        Path("docs/reports/MINIMAX_H3_TVC_BENCHMARK_20260908.md"), cases, state["summary"]
    )
    print(json.dumps(state["summary"], ensure_ascii=False), flush=True)
    return 0 if all(case.get("status") == "succeeded" for case in cases) else 2


if __name__ == "__main__":
    raise SystemExit(main())
