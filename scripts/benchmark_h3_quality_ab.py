from __future__ import annotations

import argparse
import csv
import html
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from control_plane.config import get_settings
from control_plane.h3_store import H3Store
from scripts.benchmark_h3_tvc_matrix import (
    atomic_json,
    download_media,
    extract_review_frames,
    metric_summary,
    probe_media,
)


TERMINAL = {"succeeded", "failed", "cancelled"}
QUALITIES = ("low", "medium", "high")
PRIORITY = 1
BASE_URL = "https://aicentre2.sligenai.cn:8443"
REFERENCE_VIDEO = (
    "https://oss-imgai.sligenai.cn/ai-video-kernel/20260901/"
    "ca4c8b26e6504bdaa5582d95d6d2c103.mp4"
)
SCENARIOS = {
    "watch_text": {
        "label": "智能手表·纯文本",
        "seed": 20260908101,
        "reference_video_urls": [],
        "prompt": (
            "高端智能手表完整TVC广告，深黑镜面展台，银色圆形表壳与蓝色表盘全程保持同一产品身份。"
            "开场微距展示表冠、拉丝金属和玻璃纹理，随后镜头平滑环绕，冷蓝轮廓光沿表壳流动，"
            "最后形成居中的英雄产品定格。真实商业摄影，材质精确，反射连续，镜头稳定，"
            "不变形，不复制产品，不生成文字、Logo或水印。"
        ),
    },
    "serum_video": {
        "label": "护肤精华·参考视频",
        "seed": 20260908102,
        "reference_video_urls": [REFERENCE_VIDEO],
        "prompt": (
            "以<Video 1>作为产品身份、透明材质、构图和镜头节奏参考。浅蓝科技风护肤精华TVC广告，"
            "透明磨砂精华瓶立在浅蓝水面，产品轮廓与瓶盖全程稳定一致。镜头从水面倒影平滑抬升，"
            "微小水珠和半透明分子光点环绕产品，随后缓慢推进形成正面英雄镜头。"
            "清透高级美妆商业摄影，柔和体积光，不变形，不复制产品，不生成文字、Logo、水印或手。"
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_cases(run_id: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for scenario_name, scenario in SCENARIOS.items():
        for quality in QUALITIES:
            case_id = f"{scenario_name}-{quality}"
            cases.append({
                "case_id": case_id,
                "scenario": scenario_name,
                "scenario_label": scenario["label"],
                "quality": quality,
                "status": "pending",
                "prompt": scenario["prompt"],
                "payload": {
                    "reference_video_urls": scenario["reference_video_urls"],
                    "reference_image_urls": [],
                    "reference_audio_urls": [],
                    "prompt": scenario["prompt"],
                    "duration_seconds": 10,
                    "resolution": "720p",
                    "quality": quality,
                    "aspect_ratio": "16:9",
                    "priority": PRIORITY,
                    "seed": scenario["seed"],
                    "external_ref": f"{run_id}-{case_id}",
                    "metadata": {
                        "purpose": "h3-quality-ab",
                        "run_id": run_id,
                        "scenario": scenario_name,
                        "quality": quality,
                    },
                },
            })
    return cases


def as_json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"detail": value}
    except ValueError:
        return {"detail": response.text[:500]}


def submit(client: httpx.Client, case: dict[str, Any]) -> None:
    started = time.perf_counter()
    response = client.post("/v1/video-generations/minimax-h3/jobs", json=case["payload"])
    body = as_json(response)
    case["submit_http"] = response.status_code
    case["submit_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    case["submitted_at"] = utc_now()
    if response.status_code == 202 and body.get("job_id"):
        case["job_id"] = str(body["job_id"])
        case["status"] = "queued"
        case["stage"] = "queued"
    else:
        case["status"] = "submit_failed"
        case["error"] = str(body.get("detail") or f"HTTP {response.status_code}")[:1000]


def poll(client: httpx.Client, case: dict[str, Any]) -> None:
    response = client.get(f"/v1/video-generations/minimax-h3/jobs/{case['job_id']}")
    if response.status_code != 200:
        case["poll_error"] = f"HTTP {response.status_code}: {response.text[:300]}"
        return
    job = as_json(response)
    case["status"] = str(job.get("status") or "unknown")
    case["stage"] = job.get("stage")
    case["progress"] = job.get("progress")
    case["attempt_count"] = job.get("attempt_count")
    case["last_polled_at"] = utc_now()
    if case["status"] in TERMINAL:
        case["job"] = job
        case["result_url"] = job.get("result_url")
        case["error"] = job.get("error")
        case["timing"] = job.get("timing") or {}
        case.pop("payload", None)


def build_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    succeeded = [case for case in cases if case.get("status") == "succeeded"]

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        effective = [
            float(case["timing"]["effective_processing_seconds"])
            for case in items if (case.get("timing") or {}).get("effective_processing_seconds") is not None
        ]
        return {
            "count": len(items),
            "succeeded": sum(case.get("status") == "succeeded" for case in items),
            "effective_processing_seconds": metric_summary(effective),
        }

    return {
        "generated_at": utc_now(),
        "jobs": len(cases),
        "succeeded": len(succeeded),
        "success_rate": round(len(succeeded) / len(cases), 6),
        "decode_pass_rate": round(
            sum(bool((case.get("media") or {}).get("complete_decode")) for case in succeeded)
            / max(1, len(succeeded)),
            6,
        ),
        "by_quality": {
            quality: summarize([case for case in succeeded if case["quality"] == quality])
            for quality in QUALITIES
        },
        "by_scenario": {
            name: summarize([case for case in succeeded if case["scenario"] == name])
            for name in SCENARIOS
        },
    }


def write_csv(path: Path, cases: list[dict[str, Any]]) -> None:
    columns = [
        "case_id", "scenario", "quality", "status", "job_id", "attempt_count",
        "submit_latency_ms", "queue_wait_seconds", "effective_processing_seconds",
        "end_to_end_seconds", "actual_duration_seconds", "width", "height",
        "complete_decode", "black_segments", "freeze_segments", "result_url", "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for case in cases:
            timing = case.get("timing") or {}
            media = case.get("media") or {}
            video = next(
                (item for item in media.get("streams") or [] if item.get("codec_type") == "video"), {}
            )
            writer.writerow({
                "case_id": case["case_id"], "scenario": case["scenario"],
                "quality": case["quality"], "status": case.get("status"),
                "job_id": case.get("job_id"), "attempt_count": case.get("attempt_count"),
                "submit_latency_ms": case.get("submit_latency_ms"),
                "queue_wait_seconds": timing.get("queue_wait_seconds"),
                "effective_processing_seconds": timing.get("effective_processing_seconds"),
                "end_to_end_seconds": timing.get("end_to_end_seconds"),
                "actual_duration_seconds": (media.get("format") or {}).get("duration"),
                "width": video.get("width"), "height": video.get("height"),
                "complete_decode": media.get("complete_decode"),
                "black_segments": len(media.get("black_segments") or []),
                "freeze_segments": len(media.get("freeze_starts") or []),
                "result_url": case.get("result_url"), "error": case.get("error"),
            })


def write_review(path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    cards: list[str] = []
    labels = {"low": "低档", "medium": "中档", "high": "高档"}
    for case in cases:
        timing = case.get("timing") or {}
        media = case.get("media") or {}
        frames = "".join(
            f'<img src="{html.escape(str(Path(frame).relative_to(path.parent).as_posix()))}" alt="审核帧">'
            for frame in case.get("review_frames") or []
        )
        cards.append(f"""
<article data-scenario="{case['scenario']}" data-quality="{case['quality']}">
 <h2>{html.escape(case['scenario_label'])} · {labels[case['quality']]}</h2>
 <div class="frames">{frames}</div>
 <p>有效处理 <b>{timing.get('effective_processing_seconds')}s</b> · 端到端 {timing.get('end_to_end_seconds')}s · 完整解码 {media.get('complete_decode')}</p>
 <p>黑帧段 {len(media.get('black_segments') or [])} · 冻结段 {len(media.get('freeze_starts') or [])}</p>
 <a href="{html.escape(str(case.get('result_url') or ''), quote=True)}" target="_blank">播放OSS成片</a>
</article>""")
    path.write_text(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>H3质量档位A/B</title><style>
:root{{--bg:#eef8ff;--card:#fff;--text:#14283c;--accent:#1677ff;--line:#cce4f6}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#eaf6ff,#fbfdff);font:14px/1.55 system-ui,"Microsoft YaHei";color:var(--text)}}header{{padding:25px 4vw;border-bottom:1px solid var(--line)}}h1{{margin:0}}main{{padding:24px 4vw;display:grid;grid-template-columns:repeat(3,minmax(300px,1fr));gap:16px}}article{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:15px;box-shadow:0 10px 30px #3388bb14}}h2{{font-size:17px}}.frames{{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}}img{{width:100%;height:170px;object-fit:cover}}a{{color:var(--accent);font-weight:700;text-decoration:none}}@media(max-width:1000px){{main{{grid-template-columns:1fr}}}}
</style></head><body><header><h1>MiniMax H3 low / medium / high 质量A/B</h1><p>同Prompt、同Seed、720P、10秒、16:9；6条成功 {summary.get('succeeded')}/{summary.get('jobs')}。</p></header><main>{''.join(cards)}</main></body></html>""", encoding="utf-8")


def write_report(path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# MiniMax H3 low / medium / high 质量档位A/B报告", "",
        "本次使用同Prompt、同Seed、720P、10秒、16:9，分别验证纯文本TVC与参考视频TVC。", "",
        f"成功率：{summary['succeeded']}/{summary['jobs']}；完整解码率：{summary['decode_pass_rate']}", "",
        "| 场景 | 质量 | 有效处理(s) | 排队(s) | 端到端(s) | 尺寸 | 黑帧 | 冻结 | 成片 |",
        "|---|---|---:|---:|---:|---|---:|---:|---|",
    ]
    for case in cases:
        timing = case.get("timing") or {}
        media = case.get("media") or {}
        video = next((item for item in media.get("streams") or [] if item.get("codec_type") == "video"), {})
        lines.append(
            f"| {case['scenario_label']} | {case['quality']} | {timing.get('effective_processing_seconds')} | "
            f"{timing.get('queue_wait_seconds')} | {timing.get('end_to_end_seconds')} | "
            f"{video.get('width')}×{video.get('height')} | {len(media.get('black_segments') or [])} | "
            f"{len(media.get('freeze_starts') or [])} | [OSS]({case.get('result_url')}) |"
        )
    lines.extend(["", "## 档位实现", "", "- low：较小内部画布，Euler 4步，最终交付仍为720P。", "- medium：原生720P级内部画布，Euler 4步。", "- high：与medium相同内部画布，改用res_multistep质量采样器。", "", "主观画质、产品稳定性和参考继承结论以配套三帧审核页与完整成片盲审为准。", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_video_review(path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    """Write a self-contained review page with inline, click-to-play OSS videos."""
    labels = {"low": "低档", "medium": "中档", "high": "高档"}

    def review_frame_src(frame: str) -> str:
        normalized = str(frame).replace("\\", "/")
        marker = "/review-frames/"
        if marker in normalized:
            return "review-frames/" + normalized.split(marker, 1)[1]
        try:
            return Path(frame).relative_to(path.parent).as_posix()
        except ValueError:
            return Path(frame).name

    cards: list[str] = []
    for case in cases:
        timing = case.get("timing") or {}
        media = case.get("media") or {}
        frames = case.get("review_frames") or []
        frame_urls = [
            html.escape(review_frame_src(str(frame)), quote=True)
            for frame in frames
        ]
        thumbnails = "".join(
            f'<img src="{frame_url}" loading="lazy" alt="{html.escape(case["scenario_label"])}审核帧">'
            for frame_url in frame_urls
        )
        result_url = html.escape(str(case.get("result_url") or ""), quote=True)
        poster = f' poster="{frame_urls[0]}"' if frame_urls else ""
        player = (
            f'<video controls preload="metadata" playsinline{poster} '
            f'aria-label="播放{html.escape(case["scenario_label"])}{labels[case["quality"]]}成片">'
            f'<source src="{result_url}" type="video/mp4">'
            "当前浏览器不支持视频播放，请使用下方备用链接。"
            "</video>"
            if result_url
            else '<div class="empty">暂无成片</div>'
        )
        cards.append(f"""
<article data-scenario="{case['scenario']}" data-quality="{case['quality']}">
  <h2>{html.escape(case['scenario_label'])} · {labels[case['quality']]}</h2>
  <div class="player">{player}</div>
  <div class="frames">{thumbnails}</div>
  <p>有效处理 <b>{timing.get('effective_processing_seconds')}s</b> · 端到端 {timing.get('end_to_end_seconds')}s · 完整解码 {media.get('complete_decode')}</p>
  <p>黑帧段 {len(media.get('black_segments') or [])} · 冻结段 {len(media.get('freeze_starts') or [])}</p>
  <a href="{result_url}" target="_blank" rel="noreferrer">新窗口打开 / 下载成片</a>
</article>""")
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>MiniMax H3 质量档位 A/B</title>
  <style>
    :root{{--bg:#eef8ff;--card:#fff;--text:#14283c;--accent:#1677ff;--line:#cce4f6;--muted:#52677b}}
    *{{box-sizing:border-box}}
    body{{margin:0;background:linear-gradient(145deg,#eaf6ff,#fbfdff);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif;color:var(--text)}}
    header{{padding:24px clamp(16px,4vw,56px);border-bottom:1px solid var(--line)}}
    h1{{margin:0 0 6px}} header p{{margin:0;color:var(--muted)}}
    main{{padding:24px clamp(16px,4vw,56px);display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}}
    article{{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:15px;box-shadow:0 10px 30px #3388bb14}}
    h2{{margin:0 0 12px;font-size:17px}}
    .player{{width:100%;aspect-ratio:16/9;background:#07111d;border-radius:12px;overflow:hidden}}
    video{{display:block;width:100%;height:100%;object-fit:contain;background:#07111d}}
    .frames{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;margin-top:8px}}
    img{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:5px}}
    .empty{{display:grid;place-items:center;height:100%;color:#d9e7f4}}
    p{{color:var(--muted)}} b{{color:var(--text)}}
    a{{display:inline-flex;min-height:44px;align-items:center;color:var(--accent);font-weight:700;text-decoration:none}}
    a:focus-visible{{outline:3px solid #78b5ff;outline-offset:3px;border-radius:4px}}
    @media(max-width:1100px){{main{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
    @media(max-width:720px){{main{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
  <header><h1>MiniMax H3 low / medium / high 质量 A/B</h1><p>同 Prompt、同 Seed、720P、10 秒、16:9；成功 {summary.get('succeeded')}/{summary.get('jobs')}。点击播放器即可在当前页面观看。</p></header>
  <main>{''.join(cards)}</main>
</body>
</html>"""
    path.write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--run-id", default="h3-quality-ab-20260908")
    parser.add_argument("--run-dir", type=Path, default=Path("runtime/validation/h3-quality-ab-20260908"))
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--deadline-hours", type=float, default=8)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.service_token:
        raise RuntimeError("SERVICE_TOKEN is not configured")
    store = H3Store(settings.h3_db_path)
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        cases = state["cases"]
    else:
        cases = build_cases(args.run_id)
        state = {"run_id": args.run_id, "started_at": utc_now(), "status": "running", "cases": cases}
        atomic_json(state_path, state)

    deadline = time.monotonic() + args.deadline_hours * 3600
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {settings.service_token}"},
        timeout=httpx.Timeout(60, connect=20),
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            for case in cases:
                if case.get("job_id") and case.get("status") not in TERMINAL:
                    poll(client, case)
            active_cases = [case for case in cases if case.get("job_id") and case.get("status") not in TERMINAL]
            global_active = [
                job for job in store.list_jobs(limit=500)["items"] if job.get("status") not in TERMINAL
            ]
            slots = max(0, min(3 - len(active_cases), 3 - len(global_active)))
            for case in [case for case in cases if case.get("status") == "pending"][:slots]:
                submit(client, case)
                if case.get("job_id"):
                    active_cases.append(case)
            done = sum(case.get("status") in TERMINAL or case.get("status") == "submit_failed" for case in cases)
            print(f"PROGRESS {done}/{len(cases)} active={len(active_cases)} global_active={len(global_active)}", flush=True)
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            if done == len(cases):
                break
            time.sleep(args.poll_seconds)
        else:
            raise TimeoutError("quality A/B deadline reached; jobs remain persisted for resume")

    for case in cases:
        if case.get("status") != "succeeded" or case.get("media"):
            continue
        temporary = run_dir / ".media-check" / f"{case['case_id']}.mp4"
        download_media(str(case["result_url"]), temporary)
        case["media"] = probe_media(str(temporary))
        duration = float((case["media"].get("format") or {}).get("duration") or 10)
        case["review_frames"] = extract_review_frames(
            str(temporary), run_dir / "review-frames" / case["case_id"], duration
        )
        temporary.unlink(missing_ok=True)
        atomic_json(state_path, state)

    summary = build_summary(cases)
    state.update({"status": "completed", "finished_at": utc_now(), "summary": summary})
    atomic_json(state_path, state)
    atomic_json(run_dir / "results.json", cases)
    atomic_json(run_dir / "summary.json", summary)
    write_csv(run_dir / "results.csv", cases)
    write_video_review(run_dir / "review.html", cases, summary)
    write_report(Path("docs/reports/MINIMAX_H3_QUALITY_AB_20260908.md"), cases, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["succeeded"] == summary["jobs"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
