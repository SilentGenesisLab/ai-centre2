from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from .config import Settings
from .media_fetch import VIDEO_MEDIA, download_public_media
from .upstreams import AudioUpstreams


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe(source: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [_ffmpeg().replace("ffmpeg", "ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        proc = subprocess.CompletedProcess([], 1, b"", b"ffprobe not found")
    if proc.returncode == 0:
        return json.loads(proc.stdout.decode("utf-8", errors="ignore"))

    # Some production images only include the imageio ffmpeg binary and do not
    # ship a separate ffprobe executable.  Keep the review service deployable in
    # that environment by parsing the stable human-readable ffmpeg probe output.
    try:
        fallback = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(source)], capture_output=True, check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("ffprobe failed") from exc
    text = (fallback.stderr or b"").decode("utf-8", errors="ignore")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = 0.0
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    streams: list[dict[str, Any]] = []
    video_match = re.search(r"Stream #.*?: Video:\s*([^,\s]+).*?(\d{2,6})x(\d{2,6})", text, re.S)
    if video_match:
        codec, width, height = video_match.groups()
        streams.append({"codec_type": "video", "codec_name": codec, "width": int(width), "height": int(height), "duration": duration})
    if re.search(r"Stream #.*?: Audio:", text):
        streams.append({"codec_type": "audio"})
    if not streams or duration <= 0:
        raise RuntimeError("ffprobe failed")
    return {"format": {"duration": str(duration)}, "streams": streams}


def _detect_shots(source: Path) -> list[dict[str, Any]]:
    from scenedetect import ContentDetector, detect

    scenes = detect(str(source), ContentDetector(threshold=27.0, min_scene_len=15), show_progress=False, start_in_scene=True)
    if not scenes:
        probe = _probe(source)
        duration = float(probe.get("format", {}).get("duration") or 0)
        scenes = [(0, duration)]
    result = []
    for index, item in enumerate(scenes, 1):
        if isinstance(item, tuple):
            start, end = float(item[0]), float(item[1])
        else:
            start, end = item
            start, end = float(start.get_seconds()), float(end.get_seconds())
        result.append({"shot_id": f"S{index:03d}", "start_sec": round(start, 3), "end_sec": round(end, 3), "duration_sec": round(max(0.0, end - start), 3)})
    return result


def _make_detection_proxy(source: Path, target: Path) -> Path:
    """Create a small analysis-only proxy so high-resolution sources cannot stall cut detection."""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vf", "scale=480:-2,fps=15", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", str(target)]
    subprocess.run(command, check=True, capture_output=True, timeout=300)
    return target


def _extract_frame(source: Path, target: Path, at: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(source), "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "3", str(target)]
    subprocess.run(command, check=True, capture_output=True)


def _extract_audio(source: Path, target: Path) -> None:
    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)]
    subprocess.run(command, check=True, capture_output=True)


def _frames_for_shots(source: Path, shots: list[dict[str, Any]], root: Path) -> None:
    for shot in shots:
        start, end = float(shot["start_sec"]), float(shot["end_sec"])
        points = sorted({start, round((start + end) / 2, 3), max(start, end - 0.05)})
        shot["keyframes"] = []
        for ordinal, at in enumerate(points, 1):
            path = root / "frames" / f"{shot['shot_id']}-{ordinal}.jpg"
            _extract_frame(source, path, at)
            shot["keyframes"].append({"time_sec": at, "path": str(path.relative_to(root)).replace("\\", "/")})


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _extract_json(text: str) -> dict[str, Any] | list[dict[str, Any]]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    decoder = json.JSONDecoder()
    for index, marker in enumerate(text):
        if marker not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    raise ValueError("semantic model did not return JSON")


async def _semantic_review(settings: Settings, shots: list[dict[str, Any]], profile: str, frame_root: Path | None = None) -> list[dict[str, Any]]:
    key = settings.video_review_ark_api_key.get_secret_value() if settings.video_review_ark_api_key else ""
    if not key:
        return []
    model = settings.video_review_ark_model or "doubao-seed-2-1-pro-260628"
    output: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.video_review_ark_timeout_seconds, connect=20)) as client:
        for offset in range(0, len(shots), 4):
            batch = shots[offset : offset + 4]
            content: list[dict[str, Any]] = [{"type": "input_text", "text": f"你是视频拉片分析器。只描述可观察事实，不比较参考片，不编造参数。内容类型：{profile}。返回JSON数组，每项字段：shot_id, shot_size, camera_motion, subject, action, setting, lighting_color, visual_observations, ai_artifacts, issues, confidence。issues必须是数组；每项包含category、severity、observation、evidence、impact、recommendation、confidence。没有充分证据时issues为空，不要猜测。"}]
            for shot in batch:
                content.append({"type": "input_text", "text": f"镜头 {shot['shot_id']}，时间 {shot['start_sec']:.3f}-{shot['end_sec']:.3f} 秒"})
                for frame in shot.get("keyframes", []):
                    frame_path = Path(frame["path"])
                    if frame_root is not None and not frame_path.is_absolute():
                        frame_path = frame_root / frame_path
                    content.append({"type": "input_image", "image_url": _data_url(frame_path)})
            response = await client.post(
                f"{settings.video_review_ark_base_url.rstrip('/')}/responses",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "store": settings.video_review_store_remote, "thinking": {"type": "disabled"}, "max_output_tokens": 5000, "temperature": 0, "input": [{"role": "user", "content": content}]},
            )
            response.raise_for_status()
            raw = response.json()
            text = raw.get("output_text") or "".join(c.get("text", "") for item in raw.get("output", []) for c in item.get("content", []) if isinstance(c, dict))
            parsed = _extract_json(text)
            for item in parsed if isinstance(parsed, list) else parsed.get("shots", []):
                output.append(item)
    return output


def _merge_semantics(shots: list[dict[str, Any]], semantic: list[dict[str, Any]]) -> None:
    by_id = {str(item.get("shot_id")): item for item in semantic}
    for shot in shots:
        item = by_id.get(shot["shot_id"], {})
        shot.update({k: item[k] for k in ("shot_size", "camera_motion", "subject", "action", "setting", "lighting_color", "visual_observations", "ai_artifacts", "issues", "confidence") if k in item})
        shot.setdefault("evidence_confidence", item.get("confidence", "medium" if item else "low"))


def _attach_transcript_segments(shots: list[dict[str, Any]], transcript: dict[str, Any]) -> None:
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list):
        return
    for shot in shots:
        matched = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            try:
                start = float(segment.get("start", 0))
                end = float(segment.get("end", start))
            except (TypeError, ValueError):
                continue
            if end >= float(shot["start_sec"]) and start <= float(shot["end_sec"]):
                matched.append({"start_sec": start, "end_sec": end, "text": str(segment.get("text") or "")[:1000]})
        if matched:
            shot["transcript"] = matched


def _issue_report(shots: list[dict[str, Any]], *, audio: dict[str, Any], transcript: dict[str, Any], limitations: list[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for shot in shots:
        model_issues = shot.get("issues")
        if isinstance(model_issues, list):
            for item in model_issues[:8]:
                if not isinstance(item, dict) or not str(item.get("observation") or "").strip():
                    continue
                severity = str(item.get("severity") or "minor").lower()
                if severity not in {"critical", "major", "minor", "note"}:
                    severity = "minor"
                issues.append({"id": f"Q-{len(issues)+1:03d}", "time_range": {"start_sec": shot["start_sec"], "end_sec": shot["end_sec"]}, "category": str(item.get("category") or "ai_artifact"), "severity": severity, "confidence": str(item.get("confidence") or shot.get("evidence_confidence", "medium")), "observation": str(item.get("observation")), "impact": str(item.get("impact") or "可能影响成片可用性"), "evidence": str(item.get("evidence") or f"{shot['shot_id']} 关键帧"), "recommendation": str(item.get("recommendation") or "复核对应时间段后做局部修复"), "remediation_method": "manual_review", "acceptance_checks": ["对应时间段问题消失", "主体和动作关系保持稳定"]})
        artifacts = shot.get("ai_artifacts")
        if isinstance(artifacts, list):
            for idx, artifact in enumerate(artifacts[:5], 1):
                if not artifact:
                    continue
                issues.append({"id": f"Q-{len(issues)+1:03d}", "time_range": {"start_sec": shot["start_sec"], "end_sec": shot["end_sec"]}, "category": "ai_artifact", "severity": "minor", "confidence": shot.get("evidence_confidence", "medium"), "observation": str(artifact), "impact": "可能影响画面可信度或连续性", "evidence": f"{shot['shot_id']} 关键帧", "recommendation": "复核该镜头连续帧后决定局部重生成或后期修复", "remediation_method": "manual_review", "acceptance_checks": ["相邻帧主体结构稳定", "无明显纹理爬行或融化"]})
    if transcript.get("status") == "unavailable":
        limitations.append("ASR 未返回可用文本，字幕与音画同步未完成检查")
    if audio.get("status") == "unavailable":
        limitations.append("音频分析未完成，声音问题仅能依据媒体元数据判断")
    return issues


def run_video_review(request_data: dict[str, Any], settings: Settings, job_id: str, update_state: Any) -> dict[str, Any]:
    work_dir = settings.video_review_work_dir / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    limitations: list[str] = []
    try:
        update_state("downloading", 5)
        source = download_public_media(str(request_data["video_url"]), work_dir, "source", VIDEO_MEDIA, settings.video_review_max_download_bytes, settings.video_review_download_timeout_seconds).path
        update_state("probing", 12)
        probe = _probe(source)
        video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            raise RuntimeError("video stream not found")
        duration = float(video.get("duration") or probe.get("format", {}).get("duration") or 0)
        if duration <= 0 or duration > settings.video_review_max_duration_seconds:
            raise RuntimeError(f"video duration must be between 0 and {settings.video_review_max_duration_seconds} seconds")
        update_state("detecting_shots", 25)
        detection_source = _make_detection_proxy(source, work_dir / "detection-proxy.mp4")
        shots = _detect_shots(detection_source)
        update_state("extracting_evidence", 35)
        _frames_for_shots(source, shots, work_dir)
        transcript: dict[str, Any] = {"status": "unavailable", "segments": []}
        audio: dict[str, Any] = {"status": "unavailable", "observations": []}
        if request_data.get("include_audio") or request_data.get("include_transcript"):
            audio_path = work_dir / "audio.wav"
            try:
                _extract_audio(source, audio_path)
                if request_data.get("include_transcript"):
                    content = audio_path.read_bytes()
                    transcript = asyncio.run(AudioUpstreams(settings.asr_backend_url, settings.tts_backend_url, settings.upstream_timeout_seconds).transcribe("audio.wav", content, request_data.get("language"), 5))
                    transcript = {"status": "assessed", **transcript}
                if request_data.get("include_audio"):
                    audio = {"status": "available", "duration_sec": duration, "observations": []}
            except Exception as exc:
                limitations.append(f"音频处理失败：{type(exc).__name__}")
        update_state("semantic_review", 60)
        try:
            semantic = asyncio.run(_semantic_review(settings, shots, request_data.get("analysis_profile", "advertising"), work_dir))
        except Exception as exc:
            semantic = []
            limitations.append(f"视觉语义分析失败：{type(exc).__name__}")
        _merge_semantics(shots, semantic)
        _attach_transcript_segments(shots, transcript)
        if not semantic:
            limitations.append("未配置或未获得视觉语义模型结果，镜头语义字段不可用")
        if request_data.get("continuity_check"):
            limitations.append("参考连续性对照将在语义基线稳定后启用；本次报告不改变无参考结论")
        update_state("assembling_report", 88)
        issues = _issue_report(shots, audio=audio, transcript=transcript, limitations=limitations)
        report = {
            "schema_version": "video-review.v1",
            "job_id": job_id,
            "source": {"filename": source.name, "duration_sec": round(duration, 3), "width": video.get("width"), "height": video.get("height"), "fps": video.get("r_frame_rate"), "audio_available": any(s.get("codec_type") == "audio" for s in probe.get("streams", []))},
            "summary": {"release_decision": "insufficient_evidence" if not semantic else ("post_fix" if issues else "ready"), "technical_quality": None, "goal_fit": None, "evidence_confidence": "medium" if semantic else "low", "detected_mode": "single", "detected_content_types": [request_data.get("analysis_profile", "advertising")]},
            "shots": shots,
            "transcript": transcript,
            "audio_analysis": audio,
            "issues": issues,
            "strengths": [],
            "assumptions_and_limits": limitations,
            "reference_comparison": {
                "enabled": bool(request_data.get("continuity_check")),
                "status": "not_available" if request_data.get("continuity_check") else "disabled",
                "note": "参考素材仅用于显式连续性检查，不改变无参考审片结论",
            },
            "timing": {"elapsed_seconds": round(time.perf_counter() - started, 3), "shot_count": len(shots)},
        }
        (work_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown = _render_markdown(report)
        (work_dir / "report.md").write_text(markdown, encoding="utf-8")
        (work_dir / "report.html").write_text(_render_html(report), encoding="utf-8")
        update_state("completed", 100)
        return {"job_id": job_id, "status": "succeeded", "stage": "completed", "progress": 100, "shot_count": len(shots), "report": report, "elapsed_seconds": report["timing"]["elapsed_seconds"]}
    except Exception:
        update_state("failed", 100)
        raise
    finally:
        # Keep JSON, Markdown and evidence frames for the configured retention job; only source is removed.
        for source_file in work_dir.glob("source.*"):
            source_file.unlink(missing_ok=True)
        (work_dir / "detection-proxy.mp4").unlink(missing_ok=True)
        (work_dir / "audio.wav").unlink(missing_ok=True)


def _render_markdown(report: dict[str, Any]) -> str:
    lines = ["# AI 视频拉片报告", "", f"任务：`{report['job_id']}`", f"时长：{report['source']['duration_sec']} 秒", f"镜头数：{len(report['shots'])}", "", "## 镜头表", "", "| 镜头 | 时间 | 主体 | 动作 | 证据置信度 |", "|---|---:|---|---|---|"]
    for shot in report["shots"]:
        lines.append(f"| {shot['shot_id']} | {shot['start_sec']:.3f}-{shot['end_sec']:.3f}s | {shot.get('subject','未分析')} | {shot.get('action','未分析')} | {shot.get('evidence_confidence','low')} |")
    lines += ["", "## 问题与建议", ""]
    if not report["issues"]:
        lines.append("未发现已确认问题；仍需结合完整语义模型输出和人工抽检。")
    for issue in report["issues"]:
        rng = issue["time_range"]
        lines.append(f"- **{issue['id']} [{issue['severity']}] {rng['start_sec']:.3f}-{rng['end_sec']:.3f}s**：{issue['observation']}；建议：{issue['recommendation']}")
    if report["assumptions_and_limits"]:
        lines += ["", "## 限制", "", *[f"- {item}" for item in report["assumptions_and_limits"]]]
    return "\n".join(lines) + "\n"


def _render_html(report: dict[str, Any]) -> str:
    import html

    rows = "".join(
        f"<tr><td>{html.escape(str(s['shot_id']))}</td><td>{float(s['start_sec']):.3f}–{float(s['end_sec']):.3f}s</td><td>{html.escape(str(s.get('subject') or '未分析'))}</td><td>{html.escape(str(s.get('action') or '未分析'))}</td><td>{html.escape(str(s.get('evidence_confidence') or 'low'))}</td></tr>"
        for s in report.get("shots", [])
    )
    issues = "".join(
        f"<li><b>{html.escape(str(i.get('id')))} [{html.escape(str(i.get('severity')))}]</b> {float(i['time_range']['start_sec']):.3f}–{float(i['time_range']['end_sec']):.3f}s：{html.escape(str(i.get('observation') or ''))}；建议：{html.escape(str(i.get('recommendation') or ''))}</li>"
        for i in report.get("issues", [])
    ) or "<li>未发现已确认问题；仍需结合完整语义模型输出和人工抽检。</li>"
    return f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>AI 视频拉片报告</title><style>body{{font-family:system-ui,sans-serif;max-width:1180px;margin:32px auto;padding:0 20px;color:#17324d}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #d9e4ed;padding:10px;text-align:left}}.muted{{color:#667b8c}}li{{margin:10px 0}}</style><h1>AI 视频拉片报告</h1><p class='muted'>任务 {html.escape(str(report.get('job_id')))} · 时长 {float(report['source'].get('duration_sec') or 0):.3f} 秒 · 镜头 {len(report.get('shots', []))}</p><h2>镜头表</h2><table><thead><tr><th>镜头</th><th>时间</th><th>主体</th><th>动作</th><th>置信度</th></tr></thead><tbody>{rows}</tbody></table><h2>问题与返修</h2><ul>{issues}</ul></html>"
