from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from .celery_app import celery_app
from .config import get_settings
from .media_fetch import CUBE_MEDIA, VIDEO_MEDIA, download_public_media


_SIZE_RE = re.compile(r"^LUT_3D_SIZE\s+(\d+)\s*$", re.IGNORECASE)
_VALUE_RE = re.compile(r"^\s*[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?\s+[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?\s+[-+]?\d*\.?\d+\s*$")


def _filter_path(path: Path) -> str:
    """在 FFmpeg filter 图里安全地引用一个文件路径。

    filter 解析器把 ``\\`` 当转义符、把 ``:`` 当选项分隔符，所以 Windows 的
    ``runtime\\color-grade\\<id>\\lut_clean.cube`` 会被吃成
    ``runtimecolor-grade<id>lut_clean.cube``。改用正斜杠并转义冒号后，
    相对路径和 ``C:/...`` 这类绝对路径都能正确解析。
    """
    return path.as_posix().replace(":", r"\:").replace("'", r"\'")


def _sanitize_cube(source: Path, target: Path) -> int:
    lines = source.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    size = None
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        match = _SIZE_RE.match(stripped)
        if match:
            size = int(match.group(1))
        if _VALUE_RE.match(line):
            values.append(" ".join(stripped.split()))
    if size not in {17, 33, 65}:
        raise ValueError("cube LUT must declare LUT_3D_SIZE 17, 33, or 65")
    expected = size**3
    if len(values) != expected:
        raise ValueError(f"cube LUT has {len(values)} entries; expected {expected}")
    target.write_text(
        f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n" + "\n".join(values) + "\n",
        encoding="ascii",
    )
    return size


def _upload_result(target: Path, payload: dict[str, Any], job_id: str) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    external_ref = payload.get("external_ref") or job_id
    data = {
        "external_ref": f"{external_ref}:color-grade",
        "run_id": payload.get("run_id") or "",
        "campaign_id": payload.get("campaign_id") or "",
        "project_id": payload.get("project_id") or "",
        "stage": "media.color_grade",
        "actor": "color-grade-worker",
    }
    filename = str(payload.get("filename") or "color_graded.mp4")
    with target.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data=data,
            files={"file": (filename, stream, "video/mp4")},
            timeout=httpx.Timeout(float(getattr(settings, "color_grade_upload_timeout_seconds", 900)), connect=30),
        )
    response.raise_for_status()
    result_url = str(response.json()["uri"])
    if not result_url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return result_url


@celery_app.task(bind=True, name="control_plane.color_grade", time_limit=14400, soft_time_limit=14340)
def color_grade_task(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    work_root = Path(getattr(settings, "color_grade_work_dir", "/tmp/ai-centre-color-grade"))
    max_download_bytes = int(getattr(settings, "color_grade_max_download_bytes", 2 * 1024 * 1024 * 1024))
    cube_max_bytes = int(getattr(settings, "color_grade_cube_max_bytes", 8 * 1024 * 1024))
    download_timeout = float(getattr(settings, "color_grade_download_timeout_seconds", 900))
    ffmpeg_timeout = float(getattr(settings, "color_grade_ffmpeg_timeout_seconds", 7200))
    upload_timeout = float(getattr(settings, "color_grade_upload_timeout_seconds", 900))
    work_dir = work_root / job_id
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 3})
        source = download_public_media(
            str(request_data["video_url"]), work_dir, "source", VIDEO_MEDIA,
            max_download_bytes, download_timeout,
        ).path
        cube = download_public_media(
            str(request_data["cube_url"]), work_dir, "input", CUBE_MEDIA,
            cube_max_bytes, download_timeout,
        ).path
        clean_cube = work_dir / "lut_clean.cube"
        cube_size = _sanitize_cube(cube, clean_cube)
        output = work_dir / "color_graded.mp4"
        strength = float(request_data["strength"])
        ffmpeg_bin = getattr(settings, "color_grade_ffmpeg_bin", "auto")
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe() if ffmpeg_bin == "auto" else ffmpeg_bin
        video_encoder = getattr(settings, "color_grade_video_encoder", "libx264")
        video_bitrate = getattr(settings, "color_grade_video_bitrate", "14M")
        self.update_state(state="PROGRESS", meta={"stage": "grading", "progress": 15, "lut_size": cube_size})
        filter_graph = f"[0:v]split=2[original][graded];[graded]lut3d=file={_filter_path(clean_cube)}[lut];[original][lut]blend=all_mode=normal:all_opacity={strength:.6f}[v]"
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-filter_complex", filter_graph, "-map", "[v]", "-map", "0:a?",
             "-c:v", video_encoder, "-b:v", video_bitrate,
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(output)],
            capture_output=True, text=True, timeout=ffmpeg_timeout,
            check=False,
        )
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg color grading failed: {completed.stderr.strip()[-2000:]}")
        self.update_state(state="PROGRESS", meta={"stage": "uploading", "progress": 92})
        video_url = _upload_result(output, request_data, job_id)
        return {"job_id": job_id, "status": "succeeded", "stage": "completed", "video_url": video_url,
                "strength": strength, "lut_size": cube_size, "elapsed_seconds": round(time.perf_counter() - started, 3)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
