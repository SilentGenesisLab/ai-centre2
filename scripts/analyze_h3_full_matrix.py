from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time
import wave
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import cv2
import httpx
import imageio_ffmpeg
import numpy as np

from control_plane.h3_metrics import infer_machine_type, mark_iqr_outliers


TECHNICAL_FILE = "technical-results.jsonl"
QUALITY_FILE = "quality-results.jsonl"
THEME_ENGLISH = {
    "portrait": "A young woman turns naturally beside a window with a relaxed expression, soft morning light shaping her face, slow camera push-in, realistic cinematic detail",
    "full_body_action": "A dancer performs a coherent turn and arm movement, fabric reacting naturally, stable full-body framing, realistic stage lighting",
    "product": "A premium smart watch rests on a dark pedestal, moving highlights trace the metal edge, camera orbit around the product, polished commercial style",
    "food": "Steam rises slowly from freshly baked bread as a chef cuts through the crisp crust, warm kitchen background, appetizing food commercial close-up",
    "beauty": "A transparent skincare serum bottle stands on pale blue water, ripples and tiny droplets surround it, smooth camera move, clean premium beauty commercial",
    "animal": "A golden retriever runs across grass toward sunset, fur and blades of grass moving naturally, low tracking shot, warm realistic atmosphere",
    "nature": "Morning mist moves through layered valleys as sunlight gradually illuminates the forest, slow aerial advance, grand nature documentary style",
    "city": "A modern city street after rain reflects neon lights, pedestrians and traffic move naturally, steady forward camera motion, realistic night scene",
    "architecture": "Sunlight passes through the geometric structure of a modern museum, shadows move slowly across the walls, wide lateral camera move, architectural film style",
    "science_fiction": "A future space station floats above a blue planet, mechanical lights activate in sequence, camera slowly approaches from a wide view, realistic science-fiction cinema",
    "macro": "A macro shot follows a water droplet sliding across a green leaf, crisp veins and reflections, shallow depth of field, gentle natural light changes",
    "documentary": "An artisan carefully polishes a wooden object in a quiet workshop, precise coherent hand movement, natural ambient light, observational documentary framing",
    "two_part_continuity": "A coherent realistic video with continuous subject identity, natural motion, stable camera language and a clean transition between two consecutive parts",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def percentile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def summary(values: Iterable[float]) -> dict[str, float | int | None]:
    data = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return {
        "n": len(data),
        "min": round(min(data), 3) if data else None,
        "mean": round(statistics.fmean(data), 3) if data else None,
        "stddev": round(statistics.pstdev(data), 3) if len(data) > 1 else 0.0 if data else None,
        "p50": round(percentile(data, 0.50), 3) if data else None,
        "p90": round(percentile(data, 0.90), 3) if data else None,
        "p95": round(percentile(data, 0.95), 3) if data else None,
        "p99": round(percentile(data, 0.99), 3) if data else None,
        "max": round(max(data), 3) if data else None,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=httpx.Timeout(900, connect=30), trust_env=False,
    ) as response:
        response.raise_for_status()
        with partial.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                output.write(chunk)
    partial.replace(target)


FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg_probe_and_decode(path: Path) -> dict[str, Any]:
    completed = subprocess.run([
        FFMPEG, "-hide_banner", "-i", str(path), "-f", "null", "-",
    ], capture_output=True, text=True, timeout=900, check=False)
    stderr = completed.stderr
    video_line = next((line.strip() for line in stderr.splitlines() if "Video:" in line), "")
    audio_line = next((line.strip() for line in stderr.splitlines() if "Audio:" in line), "")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    duration = None
    if duration_match:
        duration = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )
    video_codec_match = re.search(r"Video:\s*([^,\s]+)(?:\s*\(([^)]+)\))?", video_line)
    audio_codec_match = re.search(r"Audio:\s*([^,\s]+)", audio_line)
    sample_rate_match = re.search(r"(\d+)\s*Hz", audio_line)
    return {
        "decode_ok": completed.returncode == 0,
        "decode_error": stderr[-1000:] if completed.returncode != 0 else None,
        "container_duration_seconds": duration,
        "video_codec": video_codec_match.group(1) if video_codec_match else None,
        "video_profile": video_codec_match.group(2) if video_codec_match else None,
        "audio_present": bool(audio_line),
        "audio_codec": audio_codec_match.group(1) if audio_codec_match else None,
        "audio_sample_rate": int(sample_rate_match.group(1)) if sample_rate_match else None,
        "video_description": video_line,
        "audio_description": audio_line,
    }


def ratio(value: str) -> float:
    numerator, denominator = value.split(":", 1)
    return float(numerator) / float(denominator)


def analyze_frames(
    video: Path, frame_dir: Path, case_id: str, *, include_boundary: bool = False,
) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError("OpenCV could not open output")
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    positions = [(0.10, "10"), (0.50, "50"), (0.90, "90")]
    if include_boundary:
        positions.extend(((0.49, "49"), (0.51, "51")))
    target_indices = {
        max(0, min(frame_count - 1, int(round((frame_count - 1) * position)))): label
        for position, label in positions
    }
    black = 0
    frozen = 0
    flicker = 0
    blur_values: list[float] = []
    brightness: list[float] = []
    previous: np.ndarray | None = None
    decoded = 0
    frame_paths = {}
    frame_dir.mkdir(parents=True, exist_ok=True)
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        index = decoded
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
        mean = float(np.mean(small))
        brightness.append(mean)
        black += int(mean < 5.0)
        blur_values.append(float(cv2.Laplacian(small, cv2.CV_64F).var()))
        if previous is not None:
            delta = float(np.mean(cv2.absdiff(small, previous)))
            frozen += int(delta < 0.5)
            flicker += int(abs(mean - brightness[-2]) > 25.0)
        previous = small
        if index in target_indices:
            label = target_indices[index]
            target = frame_dir / f"{case_id}-{label}.jpg"
            cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            frame_paths[label] = str(target)
        decoded += 1
    capture.release()
    denominator = max(1, decoded)
    return {
        "declared_frame_count": frame_count,
        "decoded_frame_count": decoded,
        "fps": round(fps, 4), "width": width, "height": height,
        "frame_duration_seconds": round(decoded / fps, 4) if fps else None,
        "black_frame_ratio": round(black / denominator, 6),
        "frozen_transition_ratio": round(frozen / max(1, decoded - 1), 6),
        "flicker_transition_ratio": round(flicker / max(1, decoded - 1), 6),
        "blur_laplacian_mean": round(statistics.fmean(blur_values), 3) if blur_values else None,
        "blur_laplacian_p10": round(percentile(blur_values, 0.10), 3) if blur_values else None,
        "brightness_mean": round(statistics.fmean(brightness), 3) if brightness else None,
        "frame_paths": frame_paths,
    }


def analyze_audio(
    video: Path, work_dir: Path, *, boundary_seconds: float | None = None,
) -> dict[str, Any]:
    wav_path = work_dir / "audio.wav"
    completed = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
    ], capture_output=True, text=True, timeout=300, check=False)
    if completed.returncode != 0 or not wav_path.is_file():
        return {"audio_decode_ok": False, "audio_error": completed.stderr[-500:]}
    with wave.open(str(wav_path), "rb") as source:
        rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float64)
    if not len(samples):
        return {"audio_decode_ok": True, "audio_duration_seconds": 0.0, "silence_ratio": 1.0}
    normalized = samples / 32768.0
    rms = float(np.sqrt(np.mean(np.square(normalized))))
    windowed = normalized * np.hanning(len(normalized))
    spectrum = np.abs(np.fft.rfft(windowed))
    frequencies = np.fft.rfftfreq(len(normalized), d=1 / rate)
    audible = (frequencies >= 50) & (frequencies <= 4000)
    dominant = float(frequencies[audible][np.argmax(spectrum[audible])]) if np.any(audible) else None
    result = {
        "audio_decode_ok": True,
        "audio_duration_seconds": round(len(samples) / rate, 4),
        "audio_rms_dbfs": round(20 * math.log10(max(rms, 1e-12)), 3),
        "silence_ratio": round(float(np.mean(np.abs(normalized) < 10 ** (-50 / 20))), 6),
        "clipped_ratio": round(float(np.mean(np.abs(samples) >= 32760)), 8),
        "audio_dominant_frequency_hz": round(dominant, 3) if dominant is not None else None,
    }
    if boundary_seconds is not None:
        boundary = max(1, min(len(normalized) - 1, int(round(boundary_seconds * rate))))
        width = max(1, int(round(0.5 * rate)))
        before = normalized[max(0, boundary - width):boundary]
        after = normalized[boundary:min(len(normalized), boundary + width)]
        before_rms = float(np.sqrt(np.mean(np.square(before)))) if len(before) else 0.0
        after_rms = float(np.sqrt(np.mean(np.square(after)))) if len(after) else 0.0
        result.update({
            "boundary_audio_before_rms_dbfs": round(20 * math.log10(max(before_rms, 1e-12)), 3),
            "boundary_audio_after_rms_dbfs": round(20 * math.log10(max(after_rms, 1e-12)), 3),
            "boundary_audio_rms_delta_db": round(abs(
                20 * math.log10(max(after_rms, 1e-12))
                - 20 * math.log10(max(before_rms, 1e-12))
            ), 3),
            "boundary_audio_sample_jump": round(float(abs(
                normalized[boundary] - normalized[boundary - 1]
            )), 6),
        })
    return result


def expected_dimensions(record: dict[str, Any]) -> tuple[int | None, int | None]:
    output = (record.get("final") or {}).get("output") or {}
    return output.get("width"), output.get("height")


def media_analysis(run_dir: Path) -> None:
    state = load_json(run_dir / "state.json")
    destination = run_dir / TECHNICAL_FILE
    completed = {row["case_id"] for row in load_jsonl(destination)}
    frame_dir = run_dir / "frames"
    records = list(state["cases"].values())
    for index, record in enumerate(records, start=1):
        if record["case_id"] in completed:
            continue
        final = record.get("final") or {}
        result: dict[str, Any] = {
            "case_id": record["case_id"], "analyzed_at": datetime.now().astimezone().isoformat(),
            "status": record.get("status"), "technical_passed": False,
        }
        if record.get("status") in {"created", "queued", "running", "cancel_requested", "submitted"}:
            continue
        if record.get("status") != "succeeded" or not final.get("result_url"):
            result["error"] = final.get("error") or record.get("submit_error") or "no result"
            append_jsonl(destination, result)
            continue
        with tempfile.TemporaryDirectory(prefix="h3-analysis-") as temporary:
            work_dir = Path(temporary)
            video = work_dir / "output.mp4"
            try:
                download(str(final["result_url"]), video)
                probe = ffmpeg_probe_and_decode(video)
                frame_metrics = analyze_frames(
                    video, frame_dir, record["case_id"], include_boundary=record["kind"] == "two_part",
                )
                audio_metrics = (
                    analyze_audio(
                        video, work_dir,
                        boundary_seconds=(record["duration_seconds"] / 2 if record["kind"] == "two_part" else None),
                    ) if probe["audio_present"]
                    else {"audio_decode_ok": False}
                )
                duration = float(
                    frame_metrics.get("frame_duration_seconds")
                    or probe.get("container_duration_seconds") or 0
                )
                fps = float(frame_metrics.get("fps") or 0)
                expected_width, expected_height = expected_dimensions(record)
                expected_duration = float(record["duration_seconds"])
                tolerance = max(0.1, 1 / fps if fps else 0.1)
                checks = {
                    "decode": probe["decode_ok"] and frame_metrics["decoded_frame_count"] > 0,
                    "dimensions": (
                        expected_width is not None and expected_height is not None
                        and int(frame_metrics.get("width") or 0) == int(expected_width)
                        and int(frame_metrics.get("height") or 0) == int(expected_height)
                    ),
                    "duration": abs(duration - expected_duration) <= tolerance,
                    "video_codec": probe.get("video_codec") == "h264",
                    "audio_present": probe["audio_present"],
                }
                result.update({
                    "result_url": str(final["result_url"]), "file_bytes": video.stat().st_size,
                    "sha256": sha256_file(video), "duration_seconds": round(duration, 4),
                    "duration_error_seconds": round(duration - expected_duration, 4),
                    "width": int(frame_metrics.get("width") or 0),
                    "height": int(frame_metrics.get("height") or 0),
                    "fps": round(fps, 4), "video_codec": probe.get("video_codec"),
                    "video_profile": probe.get("video_profile"),
                    "audio_codec": probe.get("audio_codec"),
                    "audio_sample_rate": probe.get("audio_sample_rate"),
                    "checks": checks, "technical_passed": all(checks.values()),
                    **frame_metrics, **audio_metrics,
                })
                if record["input_mode"] in {"audio", "multimodal"}:
                    expected_frequency = {
                        1: 220.0, 2: 277.0, 3: 330.0, 4: 392.0,
                    }.get(record.get("reference_pack_index"))
                    actual_frequency = audio_metrics.get("audio_dominant_frequency_hz")
                    result["reference_audio_frequency_hz"] = expected_frequency
                    result["reference_audio_frequency_error_percent"] = (
                        round(abs(actual_frequency - expected_frequency) / expected_frequency * 100, 3)
                        if actual_frequency is not None and expected_frequency else None
                    )
                if record["kind"] == "two_part":
                    before_path = frame_metrics["frame_paths"].get("49")
                    after_path = frame_metrics["frame_paths"].get("51")
                    if before_path and after_path:
                        before_frame = cv2.imread(before_path, cv2.IMREAD_GRAYSCALE)
                        after_frame = cv2.imread(after_path, cv2.IMREAD_GRAYSCALE)
                        if before_frame is not None and after_frame is not None:
                            before_frame = cv2.resize(before_frame, (160, 90), interpolation=cv2.INTER_AREA)
                            after_frame = cv2.resize(after_frame, (160, 90), interpolation=cv2.INTER_AREA)
                            result["boundary_frame_pixel_delta"] = round(float(np.mean(
                                cv2.absdiff(before_frame, after_frame)
                            )), 4)
                if not probe["decode_ok"]:
                    result["decode_error"] = probe.get("decode_error")
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
        append_jsonl(destination, result)
        print(
            f"MEDIA {index}/{len(records)} {record['case_id']} passed={result['technical_passed']}",
            flush=True,
        )


def resolve_pack(state: dict[str, Any], record: dict[str, Any]) -> int | None:
    payload = record.get("payload") or {}
    candidates = [
        payload.get("reference_image_url"), payload.get("reference_video_url"),
        payload.get("reference_audio_url"),
    ]
    for key in ("reference_image_urls", "reference_video_urls", "reference_audio_urls"):
        candidates.extend(payload.get(key, []))
    for candidate in candidates:
        for pack in state["assets"].get("packs", []):
            if candidate in {pack.get("image_url"), pack.get("video_url"), pack.get("audio_url")}:
                return int(pack["index"])
    return None


def quality_analysis(run_dir: Path, device: str) -> None:
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModel, SiglipModel, SiglipProcessor

    state = load_json(run_dir / "state.json")
    technical = {row["case_id"]: row for row in load_jsonl(run_dir / TECHNICAL_FILE)}
    destination = run_dir / QUALITY_FILE
    completed = {row["case_id"] for row in load_jsonl(destination)}
    siglip_name = "google/siglip-base-patch16-224"
    dino_name = "facebook/dinov2-small"
    siglip_processor = SiglipProcessor.from_pretrained(siglip_name)
    siglip = SiglipModel.from_pretrained(siglip_name).to(device).eval()
    dino_processor = AutoImageProcessor.from_pretrained(dino_name)
    dino = AutoModel.from_pretrained(dino_name).to(device).eval()
    reference_cache: dict[int, Any] = {}

    def dino_embedding(image: Image.Image) -> Any:
        inputs = dino_processor(images=image, return_tensors="pt")
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.inference_mode():
            vector = dino(**inputs).last_hidden_state[:, 0]
        return torch.nn.functional.normalize(vector, dim=-1)

    records = list(state["cases"].values())
    for index, record in enumerate(records, start=1):
        case_id = record["case_id"]
        if case_id in completed:
            continue
        if case_id not in technical:
            continue
        media = technical[case_id]
        result: dict[str, Any] = {"case_id": case_id, "quality_scored": False}
        frame_paths = media.get("frame_paths") or {}
        if not media.get("technical_passed") or len(frame_paths) < 3:
            result["reason"] = "technical gate failed or frames unavailable"
            append_jsonl(destination, result)
            continue
        images = [Image.open(frame_paths[label]).convert("RGB") for label in ("10", "50", "90")]
        canonical_text = THEME_ENGLISH.get(record["theme"], record["payload"]["prompts"][0])
        siglip_inputs = siglip_processor(
            text=[canonical_text], images=images, padding="max_length", return_tensors="pt",
        )
        siglip_inputs = {key: value.to(device) for key, value in siglip_inputs.items()}
        with torch.inference_mode():
            outputs = siglip(**siglip_inputs)
            probabilities = torch.sigmoid(outputs.logits_per_image[:, 0]).detach().cpu().tolist()
        result.update({
            "quality_scored": True,
            "siglip_scores": [round(float(value), 6) for value in probabilities],
            "siglip_mean": round(statistics.fmean(probabilities), 6),
            "siglip_min": round(min(probabilities), 6),
        })
        pack_index = resolve_pack(state, record)
        if record.get("reference_compatible") is False:
            result["dino_skipped_reason"] = "reference_prompt_conflict"
        elif pack_index is not None and record["input_mode"] in {"image", "video", "multimodal"}:
            reference_path = run_dir / "assets" / f"reference-{pack_index}.png"
            if reference_path.is_file():
                if pack_index not in reference_cache:
                    reference_cache[pack_index] = dino_embedding(
                        Image.open(reference_path).convert("RGB")
                    )
                reference_vector = reference_cache[pack_index]
                similarities = []
                for image in images:
                    similarities.append(float((dino_embedding(image) @ reference_vector.T).item()))
                result["dino_reference_scores"] = [round(value, 6) for value in similarities]
                result["dino_reference_mean"] = round(statistics.fmean(similarities), 6)
                result["dino_reference_min"] = round(min(similarities), 6)
        if record["kind"] == "two_part" and all(label in frame_paths for label in ("49", "51")):
            before = dino_embedding(Image.open(frame_paths["49"]).convert("RGB"))
            after = dino_embedding(Image.open(frame_paths["51"]).convert("RGB"))
            result["two_part_boundary_dino_similarity"] = round(float((before @ after.T).item()), 6)
        append_jsonl(destination, result)
        print(f"QUALITY {index}/{len(records)} {case_id}", flush=True)


def grouped(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key))].append(row)
    output = {}
    for name, items in sorted(groups.items()):
        succeeded = [item for item in items if item.get("status") == "succeeded"]
        effective = [item for item in succeeded if not item.get("effective_outlier")]
        output[name] = {
            "count": len(items), "succeeded": len(succeeded),
            "success_rate": round(len(succeeded) / len(items), 4) if items else None,
            "wall_seconds": summary(item.get("wall_seconds") for item in succeeded),
            "effective_seconds": summary(item.get("effective_seconds") for item in effective),
            "effective_rtf": summary(item.get("effective_rtf") for item in effective),
            "outlier_count": len(succeeded) - len(effective),
        }
    return output


def parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def enrich_results(run_dir: Path, state: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    database = run_dir.parent.parent / "minimax-h3" / "h3.db"
    attempts_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if database.is_file():
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            for raw in connection.execute(
                """SELECT a.job_id,a.attempt_number,a.status,a.error,a.started_at,a.finished_at,
                          w.name AS worker_name,w.gpu_name,w.vram_total_bytes,d.machine_type
                   FROM attempts a LEFT JOIN workers w ON w.id=a.worker_id
                   LEFT JOIN managed_deployments d ON d.worker_id=a.worker_id
                   ORDER BY a.job_id,a.attempt_number"""
            ):
                attempts_by_job[str(raw["job_id"])].append(dict(raw))
        finally:
            connection.close()
    event_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in load_jsonl(run_dir / "job-events.jsonl"):
        if event.get("case_id") and event.get("event") == "state":
            event_map[str(event["case_id"])].append(event)
    state_cases = state.get("cases", {})
    for row in rows:
        record = state_cases.get(row["case_id"], {})
        final = record.get("final") or {}
        attempts = attempts_by_job.get(str(row.get("job_id")), [])
        row["attempts"] = attempts
        row["final_worker"] = attempts[-1].get("worker_name") if attempts else None
        successful_attempt = next(
            (item for item in reversed(attempts) if item.get("status") == "succeeded"),
            None,
        )
        row["machine_type"] = infer_machine_type(
            successful_attempt.get("machine_type") if successful_attempt else None,
            successful_attempt.get("gpu_name") if successful_attempt else None,
            successful_attempt.get("vram_total_bytes") if successful_attempt else None,
        )
        row["worker_names"] = sorted({
            str(item["worker_name"]) for item in attempts if item.get("worker_name")
        })
        row["failover_count"] = max(0, len(attempts) - 1)
        created = parse_datetime(final.get("created_at"))
        first_attempt = parse_datetime(attempts[0].get("started_at")) if attempts else None
        row["queue_wait_seconds"] = (
            round((first_attempt - created).total_seconds(), 3)
            if first_attempt and created else None
        )
        attempt_seconds = []
        for attempt in attempts:
            started = parse_datetime(attempt.get("started_at"))
            finished = parse_datetime(attempt.get("finished_at"))
            if started and finished:
                attempt_seconds.append(max(0.0, (finished - started).total_seconds()))
        row["attempt_seconds"] = round(sum(attempt_seconds), 3) if attempt_seconds else None
        row["failed_attempt_count"] = sum(item.get("status") == "failed" for item in attempts)
        if successful_attempt:
            successful_started = parse_datetime(successful_attempt.get("started_at"))
            successful_finished = parse_datetime(successful_attempt.get("finished_at"))
            row["effective_seconds"] = (
                round(max(0.0, (successful_finished - successful_started).total_seconds()), 3)
                if successful_started and successful_finished else None
            )
        else:
            row["effective_seconds"] = None
        row["effective_rtf"] = (
            round(float(row["effective_seconds"]) / float(row["duration_seconds"]), 6)
            if row.get("effective_seconds") is not None and float(row.get("duration_seconds") or 0) > 0
            else None
        )

        timeline = sorted(event_map.get(row["case_id"], []), key=lambda item: item["at"])
        stage_seconds: dict[str, float] = defaultdict(float)
        for current, following in zip(timeline, timeline[1:]):
            started = parse_datetime(current.get("at"))
            ended = parse_datetime(following.get("at"))
            if started and ended:
                stage_seconds[str(current.get("stage") or "unknown")] += max(
                    0.0, (ended - started).total_seconds()
                )
        row["observed_stage_seconds"] = {
            key: round(value, 3) for key, value in stage_seconds.items()
        }

    mark_iqr_outliers(
        rows,
        value_key="effective_seconds",
        bucket_keys=("resolution", "duration_seconds", "input_mode", "kind"),
    )

    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    (run_dir / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (run_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            })
    return rows


def worker_resources(samples: list[dict[str, Any]]) -> dict[str, Any]:
    controller_memory = [
        row.get("controller", {}).get("memory_used_bytes") for row in samples
        if row.get("controller", {}).get("memory_used_bytes") is not None
    ]
    controller_cpu = [
        row.get("controller", {}).get("cpu_utilization_percent") for row in samples
        if row.get("controller", {}).get("cpu_utilization_percent") is not None
    ]
    def counter_delta(name: str) -> int | None:
        values = [
            int(row.get("controller", {}).get(name)) for row in samples
            if row.get("controller", {}).get(name) is not None
        ]
        return max(0, values[-1] - values[0]) if len(values) >= 2 else None
    by_worker: dict[str, list[float]] = defaultdict(list)
    for row in samples:
        for worker in row.get("workers", []):
            total = worker.get("vram_total_bytes")
            free = worker.get("vram_free_bytes")
            if total is not None and free is not None:
                by_worker[str(worker.get("name"))].append((float(total) - float(free)) / 2**30)
    local_gpus: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in samples:
        for gpu in row.get("local_gpus", []):
            name = f"{gpu.get('index')}:{gpu.get('name')}"
            for source, target in (
                ("utilization_percent", "utilization"), ("memory_used_mib", "memory_used_mib"),
                ("temperature_c", "temperature_c"), ("power_w", "power_w"),
            ):
                if gpu.get(source) is not None:
                    local_gpus[name][target].append(float(gpu[source]))
    return {
        "sample_count": len(samples),
        "controller_cpu_utilization_percent": summary(controller_cpu),
        "controller_memory_used_gib": summary(value / 2**30 for value in controller_memory),
        "disk_read_bytes": counter_delta("disk_read_bytes_total"),
        "disk_written_bytes": counter_delta("disk_written_bytes_total"),
        "network_received_bytes": counter_delta("network_received_bytes_total"),
        "network_transmitted_bytes": counter_delta("network_transmitted_bytes_total"),
        "worker_vram_used_gib": {name: summary(values) for name, values in by_worker.items()},
        "local_gpus": {
            name: {metric: summary(values) for metric, values in metrics.items()}
            for name, metrics in local_gpus.items()
        },
    }


def report(run_dir: Path) -> None:
    state = load_json(run_dir / "state.json")
    rows = enrich_results(run_dir, state, load_json(run_dir / "results.json"))
    technical_rows = load_jsonl(run_dir / TECHNICAL_FILE)
    quality_rows = load_jsonl(run_dir / QUALITY_FILE)
    technical = {row["case_id"]: row for row in technical_rows}
    quality = {row["case_id"]: row for row in quality_rows}
    resources = worker_resources(load_jsonl(run_dir / "resource-samples.jsonl"))
    succeeded = [row for row in rows if row.get("status") == "succeeded"]
    effective_rows = [row for row in succeeded if not row.get("effective_outlier")]
    siglip_values = [
        row["siglip_mean"] for row in quality_rows if row.get("quality_scored")
    ]
    siglip_cutoff = percentile(siglip_values, 0.10)
    dino_values = [
        row["dino_reference_mean"] for row in quality_rows if row.get("dino_reference_mean") is not None
    ]
    dino_cutoff = percentile(dino_values, 0.10)
    audio_reference_errors = [
        row["reference_audio_frequency_error_percent"] for row in technical_rows
        if row.get("reference_audio_frequency_error_percent") is not None
    ]
    boundary_pixel_deltas = [
        row["boundary_frame_pixel_delta"] for row in technical_rows
        if row.get("boundary_frame_pixel_delta") is not None
    ]
    boundary_audio_deltas = [
        row["boundary_audio_rms_delta_db"] for row in technical_rows
        if row.get("boundary_audio_rms_delta_db") is not None
    ]
    boundary_dino_scores = [
        row["two_part_boundary_dino_similarity"] for row in quality_rows
        if row.get("two_part_boundary_dino_similarity") is not None
    ]
    stage_values: dict[str, list[float]] = defaultdict(list)
    for row in succeeded:
        for stage, seconds in (row.get("observed_stage_seconds") or {}).items():
            stage_values[stage].append(float(seconds))
    created_times = [
        parse_datetime((state["cases"].get(row["case_id"], {}).get("final") or {}).get("created_at"))
        for row in rows
    ]
    finished_times = [
        parse_datetime((state["cases"].get(row["case_id"], {}).get("final") or {}).get("finished_at"))
        for row in rows
    ]
    valid_created = [value for value in created_times if value]
    valid_finished = [value for value in finished_times if value]
    active_span_seconds = (
        (max(valid_finished) - min(valid_created)).total_seconds()
        if valid_created and valid_finished else None
    )
    anomalies = []
    for row in rows:
        tech = technical.get(row["case_id"], {})
        score = quality.get(row["case_id"], {})
        reasons = []
        if row.get("status") != "succeeded":
            reasons.append("generation_failed")
        if not tech.get("technical_passed"):
            reasons.append("technical_failed")
        if siglip_cutoff is not None and score.get("siglip_mean") is not None and score["siglip_mean"] <= siglip_cutoff:
            reasons.append("siglip_bottom_10_percent")
        if dino_cutoff is not None and score.get("dino_reference_mean") is not None and score["dino_reference_mean"] <= dino_cutoff:
            reasons.append("dino_bottom_10_percent")
        if tech.get("black_frame_ratio", 0) > 0.01:
            reasons.append("black_frames")
        if tech.get("frozen_transition_ratio", 0) > 0.10:
            reasons.append("frozen_frames")
        if reasons:
            anomalies.append({"case_id": row["case_id"], "reasons": reasons})
    representatives = {
        next(
            item["case_id"] for item in rows
            if item["kind"] == "single" and item["resolution"] == resolution
            and float(item["duration_seconds"]) == float(duration)
            and item["input_mode"] == input_mode and int(item["concurrency"]) == 3
        )
        for resolution in ("480p", "720p", "1080p")
        for duration in (5, 10, 15)
        for input_mode in ("text", "image", "video", "audio", "multimodal")
    }
    review_reasons: dict[str, list[str]] = {
        case_id: ["stratified_representative"] for case_id in representatives
    }
    for item in anomalies:
        review_reasons.setdefault(item["case_id"], []).extend(item["reasons"])

    metrics = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "total": len(rows), "succeeded": len(succeeded),
        "plural_contract_tasks": sum(row.get("reference_contract") == "plural" for row in rows),
        "multiple_reference_tasks": sum(
            max(
                int(row.get("reference_image_count") or 0),
                int(row.get("reference_video_count") or 0),
                int(row.get("reference_audio_count") or 0),
            ) > 1
            for row in rows
        ),
        "success_rate": round(len(succeeded) / len(rows), 4) if rows else None,
        "technical_passed": sum(row.get("technical_passed") is True for row in technical_rows),
        "technical_pass_rate": round(
            sum(row.get("technical_passed") is True for row in technical_rows) / len(rows), 4
        ) if rows else None,
        "overall_effective_seconds": summary(row.get("effective_seconds") for row in effective_rows),
        "raw_effective_seconds": summary(row.get("effective_seconds") for row in succeeded),
        "overall_wall_seconds": summary(row.get("wall_seconds") for row in succeeded),
        "overall_effective_rtf": summary(row.get("effective_rtf") for row in effective_rows),
        "effective_outlier_count": len(succeeded) - len(effective_rows),
        "queue_wait_seconds": summary(row.get("queue_wait_seconds") for row in succeeded),
        "attempt_seconds": summary(row.get("attempt_seconds") for row in succeeded),
        "submit_latency_ms": summary(row.get("submit_latency_ms") for row in rows),
        "submit_success_rate": round(
            sum(row.get("submit_http") == 202 for row in rows) / len(rows), 4
        ) if rows else None,
        "failover_count": sum(int(row.get("failover_count") or 0) for row in rows),
        "failed_attempt_count": sum(int(row.get("failed_attempt_count") or 0) for row in rows),
        "active_span_seconds": round(active_span_seconds, 3) if active_span_seconds else None,
        "tasks_per_hour": round(len(succeeded) / active_span_seconds * 3600, 3) if active_span_seconds else None,
        "generated_video_seconds_per_hour": round(
            sum(float(row["duration_seconds"]) for row in succeeded) / active_span_seconds * 3600, 3
        ) if active_span_seconds else None,
        "by_resolution": grouped(rows, "resolution"),
        "by_duration": grouped(rows, "duration_seconds"),
        "by_input_mode": grouped(rows, "input_mode"),
        "by_concurrency": grouped([row for row in rows if row["kind"] == "single"], "concurrency"),
        "by_language": grouped(rows, "language"),
        "by_aspect_ratio": grouped(rows, "aspect_ratio"),
        "by_kind": grouped(rows, "kind"),
        "by_theme": grouped(rows, "theme"),
        "by_worker": grouped(rows, "final_worker"),
        "by_machine_type": grouped(rows, "machine_type"),
        "observed_stage_seconds": {
            stage: summary(values) for stage, values in sorted(stage_values.items())
        },
        "siglip": summary(siglip_values), "siglip_bottom_10_cutoff": siglip_cutoff,
        "dino_reference": summary(dino_values), "dino_bottom_10_cutoff": dino_cutoff,
        "audio_reference_frequency_error_percent": summary(audio_reference_errors),
        "two_part_boundary_pixel_delta": summary(boundary_pixel_deltas),
        "two_part_boundary_audio_rms_delta_db": summary(boundary_audio_deltas),
        "two_part_boundary_dino_similarity": summary(boundary_dino_scores),
        "resources": resources, "anomalies": anomalies,
        "manual_review_count": len(review_reasons),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def md_group(title: str, groups: dict[str, dict[str, Any]]) -> str:
        lines = [f"### {title}", "", "| 分组 | 数量 | 成功率 | 有效样本 | 剔除 | 平均有效耗时(s) | P50 | P95 | P99 | 平均有效RTF |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for name, item in groups.items():
            effective = item["effective_seconds"]
            rtf = item["effective_rtf"]
            lines.append(
                f"| {name} | {item['count']} | {item['success_rate']:.2%} | {effective['n']} | "
                f"{item['outlier_count']} | {effective['mean']} | {effective['p50']} | "
                f"{effective['p95']} | {effective['p99']} | {rtf['mean']} |"
            )
        return "\n".join(lines)

    overall = metrics["overall_effective_seconds"]
    end_to_end = metrics["overall_wall_seconds"]
    report_text = f"""# MiniMax H3 192条全矩阵性能与质量测评报告

测试批次：`{state['run_id']}`  
正式任务：{len(rows)}条（单段180条、两段12条）  
生成成功：{len(succeeded)}/{len(rows)}（{metrics['success_rate']:.2%}）  
技术门禁通过：{metrics['technical_passed']}/{len(rows)}（{metrics['technical_pass_rate']:.2%}）
复数素材接口：{metrics['plural_contract_tasks']}条；实际使用同类多素材：{metrics['multiple_reference_tasks']}条  

## 一、总体性能

- 清洗后平均有效处理耗时：{overall['mean']}秒；P50：{overall['p50']}秒；P95：{overall['p95']}秒；P99：{overall['p99']}秒。
- 有效样本：{overall['n']}条；按“清晰度×时长×输入模式×单双段”分桶使用IQR剔除基础设施异常长尾{metrics['effective_outlier_count']}条。
- 原始端到端平均耗时：{end_to_end['mean']}秒，仅保留用于核对排队、等待Worker和基础设施中断，不作为模型性能主指标。
- 平均有效实时率RTF：{metrics['overall_effective_rtf']['mean']}，即生成1秒视频平均需要的实际处理秒数。
- 平均排队：{metrics['queue_wait_seconds']['mean']}秒；排队P95：{metrics['queue_wait_seconds']['p95']}秒；提交P95：{metrics['submit_latency_ms']['p95']}毫秒。
- 观测吞吐：{metrics['tasks_per_hour']}任务/小时、{metrics['generated_video_seconds_per_hour']}成片秒/小时；故障转移{metrics['failover_count']}次。
- 自动语义分数和参考一致性分数只用于横向比较，不能替代人工业务验收。

## 二、分层耗时与成功率

{md_group('按清晰度', metrics['by_resolution'])}

{md_group('按时长', metrics['by_duration'])}

{md_group('按输入模式', metrics['by_input_mode'])}

{md_group('按并发上限', metrics['by_concurrency'])}

{md_group('按Prompt语言', metrics['by_language'])}

{md_group('按画幅', metrics['by_aspect_ratio'])}

{md_group('按单段/两段', metrics['by_kind'])}

{md_group('按最终Worker', metrics['by_worker'])}

{md_group('按GPU机型', metrics['by_machine_type'])}

阶段耗时为5秒轮询观测值，详细分阶段统计见`summary.json`。

## 三、媒体和自动质量

- SigLIP有效样本：{metrics['siglip']['n']}；平均：{metrics['siglip']['mean']}；后10%边界：{metrics['siglip_bottom_10_cutoff']}。
- DINOv2参考一致性有效样本：{metrics['dino_reference']['n']}；平均：{metrics['dino_reference']['mean']}；后10%边界：{metrics['dino_bottom_10_cutoff']}。
- 音频参考主频误差平均：{metrics['audio_reference_frequency_error_percent']['mean']}%；P95：{metrics['audio_reference_frequency_error_percent']['p95']}%。
- 两段边界DINOv2相似度平均：{metrics['two_part_boundary_dino_similarity']['mean']}；音量突变平均：{metrics['two_part_boundary_audio_rms_delta_db']['mean']}dB。
- 技术门禁检查完整解码、尺寸、时长、H.264编码和音轨；黑帧、冻结、闪烁、模糊、静音和削波另外记录。
- 共标记{len(anomalies)}条待人工复核记录，包含生成失败、技术失败和自动评分后10%样本。

## 四、资源采样

- 资源采样数：{resources['sample_count']}，采样间隔2秒。
- 控制面CPU平均：{resources['controller_cpu_utilization_percent']['mean']}%；P95：{resources['controller_cpu_utilization_percent']['p95']}%；内存峰值：{resources['controller_memory_used_gib']['max']} GiB。
- 测试期间网络接收/发送：{round((resources['network_received_bytes'] or 0)/2**30, 3)} / {round((resources['network_transmitted_bytes'] or 0)/2**30, 3)} GiB；磁盘读/写：{round((resources['disk_read_bytes'] or 0)/2**30, 3)} / {round((resources['disk_written_bytes'] or 0)/2**30, 3)} GiB。
- 远程4090仅能取得ComfyUI显存和队列状态；GPU利用率、功耗、温度未推测。
- 5090本机GPU、控制面内存及磁盘指标详见`summary.json`。

## 五、人工审核与结论

- HTML审核页包含全部成片、Prompt、技术指标和自动评分，可按维度筛选。
- 人工审核集共{metrics['manual_review_count']}条：覆盖45个基础组合，并额外覆盖所有失败、技术异常和自动低分样本。
- 最终SLA建议、生产并发和问题排行在人工审核完成后补充到本节。

## 六、数据文件

- `results.csv` / `results.json`：逐任务性能和OSS结果。
- `{TECHNICAL_FILE}`：逐视频媒体门禁。
- `{QUALITY_FILE}`：SigLIP和DINOv2评分。
- `resource-samples.jsonl`：2秒资源采样。
- `summary.json`：报告汇总指标。
- `review.html`：全量视频审核页。
"""
    atomic_text(run_dir / "report.md", report_text)

    manual_path = run_dir / "manual-review.csv"
    existing_manual = {
        row["case_id"]: row
        for row in list(csv.DictReader(manual_path.open("r", encoding="utf-8-sig")))
    } if manual_path.is_file() else {}
    manual_fields = (
        "case_id", "selection_reason", "prompt_conformance", "reference_consistency",
        "motion_naturalness", "camera_stability", "anatomy", "visual_artifacts",
        "audio_quality", "usable", "notes",
    )
    with manual_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=manual_fields)
        writer.writeheader()
        for case_id in sorted(review_reasons):
            previous = existing_manual.get(case_id, {})
            writer.writerow({
                "case_id": case_id,
                "selection_reason": ",".join(sorted(set(review_reasons[case_id]))),
                **{field: previous.get(field, "") for field in manual_fields[2:]},
            })

    cards = []
    for row in rows:
        tech = technical.get(row["case_id"], {})
        score = quality.get(row["case_id"], {})
        anomaly = next((item for item in anomalies if item["case_id"] == row["case_id"]), None)
        cards.append(f"""
<article class="card" data-resolution="{html.escape(str(row['resolution']))}" data-duration="{row['duration_seconds']}" data-mode="{html.escape(str(row['input_mode']))}" data-concurrency="{row['concurrency']}" data-status="{'anomaly' if anomaly else 'normal'}" data-review="{'review' if row['case_id'] in review_reasons else 'other'}">
  <video controls preload="none" src="{html.escape(str(row.get('result_url') or ''))}"></video>
  <h3>{html.escape(row['case_id'])}</h3>
  <p>{html.escape(row['prompt'])}</p>
  <dl><dt>状态</dt><dd>{html.escape(str(row.get('status')))}</dd><dt>有效处理</dt><dd>{row.get('effective_seconds')}s</dd><dt>端到端</dt><dd>{row.get('wall_seconds')}s</dd><dt>有效RTF</dt><dd>{row.get('effective_rtf')}</dd><dt>IQR长尾</dt><dd>{row.get('effective_outlier')}</dd><dt>技术门禁</dt><dd>{tech.get('technical_passed')}</dd><dt>SigLIP</dt><dd>{score.get('siglip_mean')}</dd><dt>DINOv2</dt><dd>{score.get('dino_reference_mean')}</dd><dt>异常</dt><dd>{html.escape(', '.join(anomaly['reasons']) if anomaly else '-')}</dd></dl>
</article>""")
    review = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MiniMax H3 192条审核</title><style>
body{{margin:0;background:#07111f;color:#dbeafe;font:14px system-ui}}header{{position:sticky;top:0;background:#0b1729ee;padding:16px;z-index:2}}select,input{{margin:4px;padding:8px;background:#10243e;color:#dbeafe;border:1px solid #31577d;border-radius:6px}}main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px;padding:16px}}.card{{background:#0e2037;border:1px solid #24486b;border-radius:12px;padding:12px}}video{{width:100%;aspect-ratio:16/9;background:#000}}h3{{font-size:14px}}p{{height:4.5em;overflow:auto;color:#9ec9ef}}dl{{display:grid;grid-template-columns:90px 1fr;gap:4px}}dt{{color:#73b8ef}}dd{{margin:0}}.hidden{{display:none}}
</style></head><body><header><strong>MiniMax H3 全量审核</strong>
<select id="resolution"><option value="">全部清晰度</option><option>480p</option><option>720p</option><option>1080p</option></select>
<select id="duration"><option value="">全部时长</option><option value="5">5秒</option><option value="10">10秒</option><option value="15">15秒</option><option value="24">24秒</option><option value="30">30秒</option></select>
<select id="mode"><option value="">全部输入</option>{''.join(f'<option>{mode}</option>' for mode in ('text','image','video','audio','multimodal'))}</select>
<select id="concurrency"><option value="">全部并发</option>{''.join(f'<option>{value}</option>' for value in (1,3,6,12))}</select>
<select id="status"><option value="">全部状态</option><option value="anomaly">仅异常</option><option value="normal">仅正常</option></select>
<select id="review"><option value="">全部样本</option><option value="review">人工审核集</option><option value="other">非审核集</option></select>
<input id="search" placeholder="搜索Case或Prompt"></header><main>{''.join(cards)}</main><script>
const ids=['resolution','duration','mode','concurrency','status','review'];function apply(){{const q=document.querySelector('#search').value.toLowerCase();document.querySelectorAll('.card').forEach(c=>{{const ok=ids.every(id=>!document.querySelector('#'+id).value||c.dataset[id]===document.querySelector('#'+id).value)&&(!q||c.innerText.toLowerCase().includes(q));c.classList.toggle('hidden',!ok)}})}}ids.forEach(id=>document.querySelector('#'+id).addEventListener('change',apply));document.querySelector('#search').addEventListener('input',apply);
</script></body></html>"""
    atomic_text(run_dir / "review.html", review)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze and report the H3 full benchmark")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("phase", choices=("media", "quality", "report", "all"))
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if args.phase in {"media", "all"}:
        media_analysis(run_dir)
    if args.phase in {"quality", "all"}:
        quality_analysis(run_dir, args.device)
    if args.phase in {"report", "all"}:
        report(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
