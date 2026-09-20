from __future__ import annotations

import asyncio
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import oss2
import uvicorn
import yaml
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from temp_media import (
    allocate_work_directory,
    cleanup_success,
    mark_failed,
    sanitize_original_name,
)

from control_plane.media_fetch import (
    AUDIO_MEDIA,
    VIDEO_MEDIA,
    MediaFetchError,
    MediaSpec,
    download_public_media_async,
    sniff_media_suffix,
)


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}


class LipSyncUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_url: str = Field(min_length=1, max_length=4096)
    audio_url: str = Field(min_length=1, max_length=4096)
    face_restore: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_service_token(authorization: str = Header(default="")) -> None:
    token = os.environ.get("SERVICE_TOKEN", "")
    if not token or not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
        )


def checked_suffix(filename: str | None, allowed: set[str], kind: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"unsupported {kind} file type")
    return suffix


async def save_upload(upload: UploadFile, destination: Path, max_bytes: int) -> int:
    size = 0
    try:
        with destination.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail="upload is too large")
                stream.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="empty upload")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return size


async def save_typed_upload(
    upload: UploadFile,
    directory: Path,
    kind: str,
    spec: MediaSpec,
    max_bytes: int,
) -> tuple[Path, int]:
    media_id = uuid.uuid4().hex
    provisional = directory / f".{media_id}-{kind}.part"
    try:
        size = await save_upload(upload, provisional, max_bytes)
        with provisional.open("rb") as stream:
            suffix = sniff_media_suffix(stream.read(64))
        if suffix not in spec.suffixes:
            raise HTTPException(status_code=415, detail=f"unsupported {kind} file type")
        safe_name = sanitize_original_name(
            upload.filename,
            fallback=f"{kind}{suffix}",
            suffix=suffix,
        )
        target = directory / f"{media_id}-{safe_name}"
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
        os.close(descriptor)
        os.replace(provisional, target)
        target.chmod(0o660)
        return target, size
    except Exception:
        provisional.unlink(missing_ok=True)
        if "target" in locals():
            target.unlink(missing_ok=True)
        raise


class OssResultPublisher:
    def __init__(
        self,
        bucket: Any,
        public_base_url: str,
        prefix: str,
    ) -> None:
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self.prefix = prefix.strip("/")

    @classmethod
    def from_env(cls) -> OssResultPublisher | None:
        enabled = os.environ.get("MUSETALK_OSS_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        if not enabled:
            return None
        required = {
            name: os.environ.get(name, "").strip()
            for name in (
                "OSS_ACCESS_KEY_ID",
                "OSS_ACCESS_KEY_SECRET",
                "OSS_ENDPOINT",
                "OSS_BUCKET",
            )
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"missing OSS configuration: {', '.join(missing)}")
        public_base_url = os.environ.get("OSS_PUBLIC_BASE_URL", "").strip()
        if not public_base_url:
            public_base_url = (
                f"https://{required['OSS_BUCKET']}.{required['OSS_ENDPOINT']}"
            )
        auth = oss2.Auth(
            required["OSS_ACCESS_KEY_ID"],
            required["OSS_ACCESS_KEY_SECRET"],
        )
        bucket = oss2.Bucket(auth, required["OSS_ENDPOINT"], required["OSS_BUCKET"])
        return cls(
            bucket,
            public_base_url,
            os.environ.get("MUSETALK_OSS_PREFIX", "ai-centre/lipsync"),
        )

    def upload(self, job_id: str, result_path: Path) -> str:
        key = f"{self.prefix}/{job_id}/result.mp4"
        response = self.bucket.put_object_from_file(
            key,
            str(result_path),
            headers={
                "Content-Type": "video/mp4",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )
        if not 200 <= response.status < 300:
            raise RuntimeError(f"OSS upload returned HTTP {response.status}")
        return f"{self.public_base_url}/{quote(key, safe='/')}"


class MuseTalkJobs:
    def __init__(self) -> None:
        self.root = Path(os.environ.get("MUSETALK_ROOT", "/home/donxu/services/MuseTalk"))
        self.python = Path(
            os.environ.get(
                "MUSETALK_PYTHON",
                "/home/donxu/services/MuseTalk/.venv/bin/python",
            )
        )
        self.data_dir = Path(
            os.environ.get(
                "MUSETALK_DATA_DIR",
                "/home/donxu/ai-centre/runtime/musetalk/jobs",
            )
        )
        self.temp_data_root = Path(
            os.environ.get("AI_CENTRE_TEMP_DATA_ROOT", "/home/donxu/temp-data")
        )
        self.ffmpeg_dir = Path(
            os.environ.get(
                "MUSETALK_FFMPEG_PATH",
                "/home/donxu/services/MuseTalk/ffmpeg",
            )
        )
        self.timeout = int(os.environ.get("MUSETALK_JOB_TIMEOUT_SECONDS", "1800"))
        self.gfpgan_timeout = int(os.environ.get("GFPGAN_TIMEOUT_SECONDS", "1800"))
        self.gfpgan_enabled = os.environ.get("MUSETALK_GFPGAN_ENABLED", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        self.gfpgan_model = Path(
            os.environ.get(
                "GFPGAN_MODEL_PATH",
                "/home/donxu/services/GFPGAN/GFPGANv1.3.pth",
            )
        )
        self.gfpgan_weight = float(os.environ.get("GFPGAN_WEIGHT", "0.5"))
        self.torch_home = Path(
            os.environ.get(
                "MUSETALK_TORCH_HOME",
                "/home/donxu/services/MuseTalk/models/torch-cache",
            )
        )
        self.max_upload_bytes = int(
            os.environ.get("MUSETALK_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))
        )
        self.result_publisher = OssResultPublisher.from_env()
        self.oss_upload_attempts = int(
            os.environ.get("MUSETALK_OSS_UPLOAD_ATTEMPTS", "3")
        )
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._active_job: str | None = None
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        pending_jobs: list[dict[str, Any]] = []
        for status_path in self.data_dir.glob("*/status.json"):
            try:
                job = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("state") == "queued":
                pending_jobs.append(job)
            elif job.get("state") == "running":
                self._recover_running_job(job, status_path.parent)
                pending_jobs.append(job)
        for job in sorted(pending_jobs, key=lambda item: item.get("created_at") or ""):
            self._queue.put(str(job["job_id"]))
        self._worker = threading.Thread(target=self._work, name="musetalk-worker", daemon=True)
        self._worker.start()

    def _recover_running_job(self, job: dict[str, Any], job_dir: Path) -> None:
        recovery_count = int(job.get("recovery_count", 0)) + 1
        previous_stage = str(job.get("stage") or "running")
        for log_name in ("inference.log", "gfpgan.log"):
            log_path = job_dir / log_name
            if log_path.is_file():
                archived = job_dir / f"{log_path.stem}.recovery-{recovery_count}.log"
                os.replace(log_path, archived)
        temp_dir = self._job_temp_dir(job, job_dir)
        shutil.rmtree(temp_dir / "output", ignore_errors=True)
        for output_dir in temp_dir.glob("*-output"):
            shutil.rmtree(output_dir, ignore_errors=True)
        (temp_dir / "musetalk-result.mp4").unlink(missing_ok=True)
        for path in temp_dir.glob("*-musetalk-result.mp4"):
            path.unlink(missing_ok=True)
        for name in ("inference.yaml", "result.mp4"):
            (job_dir / name).unlink(missing_ok=True)
        for timing in ("elapsed_seconds", "musetalk_seconds", "gfpgan_seconds"):
            job.pop(timing, None)
        job.update(
            state="queued",
            stage="queued",
            started_at=None,
            finished_at=None,
            error=None,
            recovery_count=recovery_count,
            recovered_at=utc_now(),
            recovered_from_stage=previous_stage,
        )
        self._write_status(job)

    def stop(self) -> None:
        with self._lock:
            processes = list(self._processes.items())
        for job_id, process in processes:
            self._cancelled.add(job_id)
            self._terminate(process)
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=10)

    def submit(
        self,
        job_id: str,
        video_name: str,
        audio_name: str,
        video_bytes: int,
        audio_bytes: int,
        face_restore: bool,
        *,
        temp_dir: Path | None = None,
        video_temp_name: str | None = None,
        audio_temp_name: str | None = None,
    ) -> dict[str, Any]:
        job = {
            "job_id": job_id,
            "state": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "video_filename": video_name,
            "audio_filename": audio_name,
            "video_bytes": video_bytes,
            "audio_bytes": audio_bytes,
            "face_restore": face_restore,
            "temp_dir": str(temp_dir) if temp_dir is not None else None,
            "video_temp_name": video_temp_name,
            "audio_temp_name": audio_temp_name,
            "stage": "queued",
            "result_url": f"/v1/lipsync/jobs/{job_id}/video",
            "error": None,
        }
        self._write_status(job)
        self._queue.put(job_id)
        return job

    def status(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        path = self.data_dir / job_id / "status.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="lip-sync job not found") from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail="lip-sync job status is corrupt") from exc

    def list_jobs(self, limit: int, state: str | None = None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for status_path in self.data_dir.glob("*/status.json"):
            try:
                job = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state and job.get("state") != state:
                continue
            items.append(job)
        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {"jobs": items[:limit], "total": len(items), "limit": limit}

    def logs(self, job_id: str, stage: str, tail: int) -> dict[str, Any]:
        self._validate_job_id(job_id)
        names = {
            "musetalk": "inference.log",
            "gfpgan": "gfpgan.log",
        }
        try:
            filename = names[stage]
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="unsupported log stage") from exc
        path = self.data_dir / job_id / filename
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                lines = list(deque(stream, maxlen=tail))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job log not found") from exc
        return {
            "job_id": job_id,
            "stage": stage,
            "lines": len(lines),
            "log": "".join(lines),
        }

    def result_path(self, job_id: str) -> Path:
        job = self.status(job_id)
        if job["state"] != "completed":
            raise HTTPException(status_code=409, detail="lip-sync job is not complete")
        result = self.data_dir / job_id / "result.mp4"
        if not result.is_file():
            raise HTTPException(status_code=500, detail="lip-sync result is missing")
        return result

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.status(job_id)
        if job["state"] in TERMINAL_STATES:
            return job
        self._cancelled.add(job_id)
        with self._lock:
            process = self._processes.get(job_id)
        if process:
            self._terminate(process)
        job.update(state="cancelled", finished_at=utc_now(), error=None)
        self._write_status(job)
        if job.get("temp_dir"):
            mark_failed(self._job_temp_dir(job), "lip-sync job cancelled")
        return job

    def health(self) -> dict[str, Any]:
        required = (
            "models/musetalkV15/unet.pth",
            "models/musetalkV15/musetalk.json",
            "models/dwpose/dw-ll_ucoco_384.pth",
            "models/face-parse-bisent/79999_iter.pth",
            "models/face-parse-bisent/resnet18-5c106cde.pth",
            "models/sd-vae/config.json",
            "models/sd-vae/diffusion_pytorch_model.bin",
            "models/whisper/config.json",
            "models/whisper/pytorch_model.bin",
            "models/whisper/preprocessor_config.json",
            "ffmpeg/ffmpeg",
            "gfpgan/weights/detection_Resnet50_Final.pth",
            "gfpgan/weights/parsing_parsenet.pth",
            "models/torch-cache/hub/checkpoints/s3fd-619a316812.pth",
        )
        missing = [path for path in required if not (self.root / path).is_file()]
        if self.gfpgan_enabled and not self.gfpgan_model.is_file():
            missing.append(str(self.gfpgan_model))
        configured = self.root.is_dir() and self.python.is_file() and not missing
        return {
            "status": "ok" if configured else "not_configured",
            "configured": configured,
            "active_job": self._active_job,
            "queued_jobs": self._queue.qsize(),
            "max_concurrency": 1,
            "gpu": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
            "gfpgan_enabled": self.gfpgan_enabled,
            "oss_result_upload_enabled": self.result_publisher is not None,
            "missing_artifacts": missing,
        }

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            if job_id in self._cancelled:
                continue
            try:
                self._run_job(job_id)
            except Exception as exc:
                job = self.status(job_id)
                if job["state"] not in TERMINAL_STATES:
                    job.update(
                        state="failed",
                        error=f"{type(exc).__name__}: {exc}",
                        finished_at=utc_now(),
                    )
                    self._write_status(job)
                    if job.get("temp_dir"):
                        mark_failed(self._job_temp_dir(job), exc)

    def _run_job(self, job_id: str) -> None:
        job_dir = self.data_dir / job_id
        job = self.status(job_id)
        if job["state"] != "queued":
            return
        job.update(state="running", stage="musetalk", started_at=utc_now())
        self._write_status(job)
        self._active_job = job_id
        temp_dir = self._job_temp_dir(job, job_dir)
        video_path = self._job_input_path(job, temp_dir, "video")
        audio_path = self._job_input_path(job, temp_dir, "audio")
        result_name = f"{uuid.uuid4().hex}-result.mp4"
        config_path = job_dir / "inference.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "task_0": {
                        "video_path": str(video_path),
                        "audio_path": str(audio_path),
                        "result_name": result_name,
                    }
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        output_dir = temp_dir / f"{uuid.uuid4().hex}-output"
        output_dir.mkdir(mode=0o770)
        log_path = job_dir / "inference.log"
        command = [
            str(self.python),
            "-m",
            "scripts.inference",
            "--inference_config",
            str(config_path),
            "--result_dir",
            str(output_dir),
            "--unet_model_path",
            "models/musetalkV15/unet.pth",
            "--unet_config",
            "models/musetalkV15/musetalk.json",
            "--version",
            "v15",
            "--ffmpeg_path",
            str(self.ffmpeg_dir),
            "--gpu_id",
            "0",
            "--use_float16",
        ]
        started = time.monotonic()
        try:
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=self.root,
                    env={
                        **os.environ,
                        "CUDA_VISIBLE_DEVICES": "0",
                        "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1",
                        "TORCH_HOME": str(self.torch_home),
                    },
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                with self._lock:
                    self._processes[job_id] = process
                try:
                    return_code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    raise TimeoutError(f"inference exceeded {self.timeout} seconds")
            if job_id in self._cancelled:
                return
            if return_code != 0:
                raise RuntimeError(f"MuseTalk exited with code {return_code}; see inference.log")
            candidates = list(output_dir.rglob(result_name))
            if not candidates:
                candidates = list(output_dir.rglob("*.mp4"))
            if not candidates:
                raise RuntimeError("MuseTalk did not produce an MP4 result")
            musetalk_seconds = round(time.monotonic() - started, 3)
            if job.get("face_restore") and self.gfpgan_enabled:
                raw_result = temp_dir / f"{uuid.uuid4().hex}-musetalk-result.mp4"
                shutil.move(str(candidates[0]), raw_result)
                job.update(stage="gfpgan", musetalk_seconds=musetalk_seconds)
                self._write_status(job)
                gfpgan_started = time.monotonic()
                gfpgan_command = [
                    str(self.python),
                    "-m",
                    "managed_backends.gfpgan_video",
                    "--input",
                    str(raw_result),
                    "--output",
                    str(job_dir / "result.mp4"),
                    "--model-path",
                    str(self.gfpgan_model),
                    "--ffmpeg",
                    str(self.ffmpeg_dir / "ffmpeg"),
                    "--weight",
                    str(self.gfpgan_weight),
                ]
                with (job_dir / "gfpgan.log").open("w", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        gfpgan_command,
                        cwd=self.root,
                        env={
                            **os.environ,
                            "CUDA_VISIBLE_DEVICES": "0",
                            "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1",
                            "TORCH_HOME": str(self.torch_home),
                        },
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                    )
                    with self._lock:
                        self._processes[job_id] = process
                    try:
                        gfpgan_return_code = process.wait(timeout=self.gfpgan_timeout)
                    except subprocess.TimeoutExpired:
                        self._terminate(process)
                        raise TimeoutError(
                            f"GFPGAN exceeded {self.gfpgan_timeout} seconds"
                        )
                if job_id in self._cancelled:
                    return
                if gfpgan_return_code != 0:
                    raise RuntimeError(
                        f"GFPGAN exited with code {gfpgan_return_code}; see gfpgan.log"
                    )
                gfpgan_seconds = round(time.monotonic() - gfpgan_started, 3)
            else:
                shutil.move(str(candidates[0]), job_dir / "result.mp4")
                gfpgan_seconds = 0.0
            result_url = job["result_url"]
            if self.result_publisher is not None:
                job.update(stage="uploading")
                self._write_status(job)
                result_url = self._upload_result(job_id, job_dir / "result.mp4")
            job.update(
                state="completed",
                stage="completed",
                finished_at=utc_now(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                musetalk_seconds=musetalk_seconds,
                gfpgan_seconds=gfpgan_seconds,
                result_url=result_url,
                result_storage="oss" if self.result_publisher is not None else "local",
                oss_uploaded_at=utc_now() if self.result_publisher is not None else None,
                error=None,
            )
            self._write_status(job)
            if job.get("temp_dir"):
                cleanup_success(temp_dir)
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
            self._active_job = None

    def _job_temp_dir(self, job: dict[str, Any], legacy_dir: Path | None = None) -> Path:
        raw = job.get("temp_dir")
        if not raw:
            return legacy_dir or (self.data_dir / str(job["job_id"]))
        path = Path(str(raw)).resolve(strict=False)
        try:
            path.relative_to(self.temp_data_root.resolve(strict=False))
        except ValueError as exc:
            raise RuntimeError("lip-sync temporary path is outside the configured root") from exc
        return path

    @staticmethod
    def _job_input_path(job: dict[str, Any], temp_dir: Path, kind: str) -> Path:
        name = job.get(f"{kind}_temp_name")
        if name:
            path = temp_dir / str(name)
            if path.parent != temp_dir or not path.is_file():
                raise RuntimeError(f"lip-sync {kind} input is missing")
            return path
        try:
            return next(temp_dir.glob(f"input-{kind}.*"))
        except StopIteration as exc:
            raise RuntimeError(f"lip-sync {kind} input is missing") from exc

    def _upload_result(self, job_id: str, result_path: Path) -> str:
        if self.result_publisher is None:
            raise RuntimeError("OSS result publisher is disabled")
        attempts = max(1, self.oss_upload_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return self.result_publisher.upload(job_id, result_path)
            except Exception as exc:
                if attempt == attempts:
                    raise RuntimeError(
                        f"OSS result upload failed after {attempts} attempts: "
                        f"{type(exc).__name__}"
                    ) from exc
                time.sleep(2 ** (attempt - 1))
        raise AssertionError("unreachable")

    def _write_status(self, job: dict[str, Any]) -> None:
        job_dir = self.data_dir / str(job["job_id"])
        job_dir.mkdir(parents=True, exist_ok=True)
        temporary = job_dir / ".status.json.tmp"
        temporary.write_text(
            json.dumps(job, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, job_dir / "status.json")

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        try:
            parsed = uuid.UUID(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="lip-sync job not found") from exc
        if str(parsed) != job_id:
            raise HTTPException(status_code=404, detail="lip-sync job not found")

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)


jobs = MuseTalkJobs()


@asynccontextmanager
async def lifespan(_: FastAPI):
    jobs.start()
    try:
        yield
    finally:
        jobs.stop()


app = FastAPI(title="AI Centre 2 MuseTalk", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    details = jobs.health()
    if not details["configured"]:
        raise HTTPException(status_code=503, detail=details)
    return details


@app.post(
    "/v1/lipsync/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_url_job(
    request: LipSyncUrlRequest,
) -> dict[str, Any]:
    if request.face_restore and not jobs.gfpgan_enabled:
        raise HTTPException(status_code=503, detail="GFPGAN face restoration is disabled")
    job_id = str(uuid.uuid4())
    temp_dir = allocate_work_directory(f"musetalk-{job_id}", root=jobs.temp_data_root)
    downloads = await asyncio.gather(
        download_public_media_async(
            request.video_url,
            temp_dir,
            f"{uuid.uuid4().hex}-input-video",
            VIDEO_MEDIA,
            jobs.max_upload_bytes,
            jobs.timeout,
        ),
        download_public_media_async(
            request.audio_url,
            temp_dir,
            f"{uuid.uuid4().hex}-input-audio",
            AUDIO_MEDIA,
            jobs.max_upload_bytes,
            jobs.timeout,
        ),
        return_exceptions=True,
    )
    failure = next((item for item in downloads if isinstance(item, Exception)), None)
    if failure:
        mark_failed(temp_dir, failure)
        if isinstance(failure, MediaFetchError):
            raise HTTPException(failure.status_code, failure.detail) from failure
        raise HTTPException(502, "unable to download lip-sync media") from failure
    video_result, audio_result = downloads
    try:
        return jobs.submit(
            job_id,
            video_result.path.name,
            audio_result.path.name,
            video_result.size,
            audio_result.size,
            request.face_restore,
            temp_dir=temp_dir,
            video_temp_name=video_result.path.name,
            audio_temp_name=audio_result.path.name,
        )
    except Exception as exc:
        mark_failed(temp_dir, exc)
        raise


@app.post(
    "/v1/lipsync/jobs/upload",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_token)],
)
async def create_upload_job(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    face_restore: bool = Form(default=False),
) -> dict[str, Any]:
    if face_restore and not jobs.gfpgan_enabled:
        raise HTTPException(status_code=503, detail="GFPGAN face restoration is disabled")
    job_id = str(uuid.uuid4())
    temp_dir = allocate_work_directory(f"musetalk-{job_id}", root=jobs.temp_data_root)
    try:
        video_path, video_bytes = await save_typed_upload(
            video,
            temp_dir,
            "video",
            VIDEO_MEDIA,
            jobs.max_upload_bytes,
        )
        audio_path, audio_bytes = await save_typed_upload(
            audio,
            temp_dir,
            "audio",
            AUDIO_MEDIA,
            jobs.max_upload_bytes,
        )
    except Exception as exc:
        mark_failed(temp_dir, exc)
        raise
    try:
        return jobs.submit(
            job_id,
            video.filename or video_path.name,
            audio.filename or audio_path.name,
            video_bytes,
            audio_bytes,
            face_restore,
            temp_dir=temp_dir,
            video_temp_name=video_path.name,
            audio_temp_name=audio_path.name,
        )
    except Exception as exc:
        mark_failed(temp_dir, exc)
        raise


@app.get(
    "/v1/lipsync/jobs",
    dependencies=[Depends(require_service_token)],
)
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    state: str | None = Query(default=None),
) -> dict[str, Any]:
    return jobs.list_jobs(limit, state)


@app.get(
    "/v1/lipsync/jobs/{job_id}",
    dependencies=[Depends(require_service_token)],
)
def get_job(job_id: str) -> dict[str, Any]:
    return jobs.status(job_id)


@app.get(
    "/v1/lipsync/jobs/{job_id}/video",
    dependencies=[Depends(require_service_token)],
)
def get_video(job_id: str) -> FileResponse:
    return FileResponse(
        jobs.result_path(job_id),
        media_type="video/mp4",
        filename=f"{job_id}.mp4",
    )


@app.get(
    "/v1/lipsync/jobs/{job_id}/logs",
    dependencies=[Depends(require_service_token)],
)
def get_logs(
    job_id: str,
    stage: str = Query(default="musetalk"),
    tail: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return jobs.logs(job_id, stage, tail)


@app.post(
    "/v1/lipsync/jobs/{job_id}/cancel",
    dependencies=[Depends(require_service_token)],
)
def cancel_job(job_id: str) -> dict[str, Any]:
    return jobs.cancel(job_id)


if __name__ == "__main__":
    uvicorn.run(
        "managed_backends.musetalk_api:app",
        host=os.environ.get("MUSETALK_HOST", "127.0.0.1"),
        port=int(os.environ.get("MUSETALK_PORT", "9011")),
        workers=1,
    )
