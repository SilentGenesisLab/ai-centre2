from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from .engine import PaddleOcrEngine
from .schemas import AsrSegment, ImageInput, Region, SubtitleDetectConfig


PROCESS_TYPES = {"top_title", "bottom_subtitle", "cta_text"}
TYPE_COLORS = {
    "top_title": (255, 120, 0),
    "bottom_subtitle": (0, 210, 0),
    "cta_text": (0, 145, 255),
    "product_text": (140, 140, 140),
    "logo_watermark": (140, 140, 140),
    "unknown_text": (210, 0, 210),
}
MODE_INTERVALS = {"fast": 0.30, "balanced": 0.20, "accurate": 0.10}


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    frame_count: int

    @property
    def duration(self) -> float:
        return self.frame_count / self.fps


@dataclass
class TextGroup:
    frame_index: int
    shot_id: int
    bbox: list[int]
    line_bboxes: list[list[int]]
    text: str
    score: float
    line_count: int
    median_line_height: float
    type: str
    reason: str = ""


@dataclass
class Sample:
    frame_index: int
    shot_id: int
    raw_items: list[dict[str, Any]]
    groups: list[TextGroup]


@dataclass
class Track:
    track_id: str
    type: str
    shot_id: int
    observations: list[TextGroup] = field(default_factory=list)
    stable_bbox: list[int] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class Event:
    event_id: str
    track_id: str
    type: str
    shot_id: int
    observations: list[TextGroup]
    start_frame: int
    end_frame: int
    source_text: str
    line_count: int
    line_bboxes: list[list[int]]
    stable_bbox: list[int]
    ocr_confidence: float
    asr_overlap: float | None
    needs_review: bool
    color_block: dict[str, Any] | None = None


def detect_subtitle_events(
    engine: PaddleOcrEngine,
    input_path: Path,
    output_dir: Path,
    source_lang_hint: str | None,
    mode: str,
    export_debug_video: bool,
    config: SubtitleDetectConfig,
    asr_segments: list[AsrSegment],
) -> dict[str, Any]:
    started = time.perf_counter()
    input_path = input_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    info = _probe_video(input_path)
    interval_seconds = config.coarse_interval_seconds or MODE_INTERVALS[mode]
    interval_frames = max(1, round(info.fps * interval_seconds))

    with tempfile.TemporaryDirectory(prefix="precise_subtitle_") as temp_name:
        temp_dir = Path(temp_name)
        samples, shots = _coarse_scan(
            engine=engine,
            info=info,
            temp_dir=temp_dir,
            interval_frames=interval_frames,
            source_lang_hint=source_lang_hint,
            config=config,
        )
        tracks = _build_tracks(samples, info, interval_frames, config)
        events = _build_events(tracks, info, interval_frames, config, asr_segments)
        events = _consolidate_events(events, info, config)
        _refine_event_boundaries(
            engine=engine,
            info=info,
            events=events,
            temp_dir=temp_dir,
            source_lang_hint=source_lang_hint,
            interval_frames=interval_frames,
            refine_step=2 if mode == "fast" else 1,
            min_score=config.min_ocr_score,
        )
        events = _consolidate_events(events, info, config)
        _attach_color_blocks(info, events)

    ignored_regions = _collect_ignored_regions(samples)
    event_payloads = [_event_payload(event, info) for event in events]
    track_payloads = [_track_payload(track) for track in tracks]
    status, qa = _build_qa(info, events, ignored_regions)
    elapsed = time.perf_counter() - started
    result = {
        "schema_version": "1.0",
        "job_id": f"subtitle-{uuid.uuid4().hex[:12]}",
        "status": status,
        "video": {
            "path": str(info.path),
            "width": info.width,
            "height": info.height,
            "fps": round(info.fps, 6),
            "frame_count": info.frame_count,
            "duration": round(info.duration, 6),
        },
        "sampling": {
            "mode": mode,
            "coarse_interval_frames": interval_frames,
            "coarse_interval_seconds": round(interval_frames / info.fps, 6),
            "boundary_refine_step_frames": 1,
            "boundary_window_seconds": config.boundary_window_seconds,
        },
        "shots": shots,
        "tracks": track_payloads,
        "events": event_payloads,
        "ignored_regions": ignored_regions,
        "qa": qa,
        "metrics": {
            "shots": len(shots),
            "tracks": len(tracks),
            "events": len(events),
            "needs_review": sum(event.needs_review for event in events),
            "elapsed_seconds": round(elapsed, 3),
        },
    }
    events_json = output_dir / "subtitle_events.json"
    qa_json = output_dir / "qa_report.json"
    events_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    qa_json.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")

    outputs: dict[str, str | None] = {
        "raw_ocr": None,
        "classified_tracks": None,
        "white_mask": None,
        "stable_bbox": None,
        "review_html": None,
    }
    if export_debug_video:
        outputs = _render_debug_outputs(info, samples, events, output_dir)
        review_path = _write_review_html(info.path, output_dir, outputs)
        outputs["review_html"] = str(review_path)

    return {
        "job_id": result["job_id"],
        "status": status,
        "output_dir": str(output_dir),
        "events_json": str(events_json),
        "qa_json": str(qa_json),
        "debug_video": outputs["stable_bbox"],
        "white_mask_video": outputs["white_mask"],
        "review_html": outputs["review_html"],
        "metrics": result["metrics"],
    }


def _probe_video(path: Path) -> VideoInfo:
    if not path.is_file():
        raise FileNotFoundError(path)
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {path}")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    capture.release()
    if width <= 0 or height <= 0 or fps <= 0 or frame_count <= 0:
        raise RuntimeError(f"invalid video metadata: {path}")
    return VideoInfo(path=path, width=width, height=height, fps=fps, frame_count=frame_count)


def _coarse_scan(
    engine: PaddleOcrEngine,
    info: VideoInfo,
    temp_dir: Path,
    interval_frames: int,
    source_lang_hint: str | None,
    config: SubtitleDetectConfig,
) -> tuple[list[Sample], list[dict[str, Any]]]:
    sample_indices = set(range(0, info.frame_count, interval_frames))
    sample_indices.add(info.frame_count - 1)
    shot_starts = [0]
    capture = cv2.VideoCapture(str(info.path))
    previous_hist: np.ndarray | None = None
    for frame_index in range(info.frame_count):
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % max(1, round(info.fps / 10.0)) != 0:
            continue
        hist = _frame_histogram(frame)
        if previous_hist is not None:
            distance = cv2.compareHist(previous_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
            if distance >= config.scene_cut_threshold and frame_index - shot_starts[-1] >= round(0.25 * info.fps):
                shot_starts.append(frame_index)
                sample_indices.add(frame_index)
                sample_indices.add(max(0, frame_index - 1))
        previous_hist = hist
    capture.release()
    shot_starts = sorted(set(shot_starts))
    shots = _make_shots(shot_starts, info)

    sorted_indices = sorted(sample_indices)
    raw_by_frame: dict[int, list[dict[str, Any]]] = {}
    capture = cv2.VideoCapture(str(info.path))
    for batch_start in range(0, len(sorted_indices), 32):
        image_inputs: list[ImageInput] = []
        for frame_index in sorted_indices[batch_start : batch_start + 32]:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                continue
            image_path = temp_dir / f"coarse_{frame_index:06d}.jpg"
            if not cv2.imwrite(str(image_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 94]):
                raise RuntimeError(f"failed to write OCR frame: {image_path}")
            image_inputs.append(
                ImageInput(
                    image_id=str(frame_index),
                    path=str(image_path),
                    time=frame_index / info.fps,
                    regions=[Region(name="full", bbox=None)],
                )
            )
        if not image_inputs:
            continue
        results = engine.recognize_batch(image_inputs)
        for result in results:
            raw_by_frame[int(result.image_id)] = [
                {"bbox": item.bbox, "text": item.text, "score": item.score}
                for item in result.items
            ]
    capture.release()

    samples: list[Sample] = []
    for frame_index in sorted_indices:
        shot_id = _shot_id_for_frame(frame_index, shots)
        raw_items = raw_by_frame.get(frame_index, [])
        groups = group_and_classify_frame(
            frame_index=frame_index,
            shot_id=shot_id,
            raw_items=raw_items,
            width=info.width,
            height=info.height,
            config=config,
        )
        samples.append(Sample(frame_index=frame_index, shot_id=shot_id, raw_items=raw_items, groups=groups))
    return samples, shots


def _frame_histogram(frame: np.ndarray) -> np.ndarray:
    small = cv2.resize(frame, (96, 54), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 24], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def _make_shots(starts: list[int], info: VideoInfo) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else info.frame_count - 1
        shots.append(
            {
                "shot_id": index + 1,
                "start_frame": start,
                "end_frame": end,
                "start_time": round(start / info.fps, 6),
                "end_time": round(end / info.fps, 6),
            }
        )
    return shots


def _shot_id_for_frame(frame_index: int, shots: list[dict[str, Any]]) -> int:
    for shot in shots:
        if shot["start_frame"] <= frame_index <= shot["end_frame"]:
            return int(shot["shot_id"])
    return int(shots[-1]["shot_id"])


def group_and_classify_frame(
    frame_index: int,
    shot_id: int,
    raw_items: list[dict[str, Any]],
    width: int,
    height: int,
    config: SubtitleDetectConfig,
) -> list[TextGroup]:
    candidates: list[dict[str, Any]] = []
    for item in raw_items:
        bbox = _clamp_bbox(item["bbox"], width, height)
        x1, y1, x2, y2 = bbox
        box_height = y2 - y1
        box_width = x2 - x1
        if item["score"] < config.min_ocr_score or box_width <= 1 or box_height <= 1:
            continue
        candidates.append({**item, "bbox": bbox})
    if not candidates:
        return []

    line_groups = _merge_same_line(candidates, width, height)
    text_groups = _merge_multiline(line_groups, width, height)
    classified: list[TextGroup] = []
    for group in text_groups:
        bbox = group["bbox"]
        x1, y1, x2, y2 = bbox
        box_cy = (y1 + y2) / 2
        center_ratio = (x1 + x2) / width
        median_height = float(np.median([line[3] - line[1] for line in group["line_bboxes"]]))
        dense_small = len(group["source_items"]) >= 5 and median_height < 0.028 * height
        lower_dense_block = (
            box_cy >= 0.52 * height
            and len(group["line_bboxes"]) >= 3
            and median_height < 0.035 * height
        )
        width_ratio = (x2 - x1) / width
        if dense_small:
            group_type, reason = "product_text", "dense_small_text"
        elif lower_dense_block:
            group_type, reason = "product_text", "lower_dense_multiline_text"
        elif median_height < 0.018 * height and len(group["line_bboxes"]) == 1:
            group_type, reason = "product_text", "single_small_text"
        elif box_cy < 0.03 * height or box_cy > 0.95 * height:
            group_type, reason = "logo_watermark", "edge_text"
        elif (
            0.03 * height <= box_cy < 0.48 * height
            and abs(center_ratio - 1.0) <= config.center_tolerance_top
            and width_ratio >= 0.18
        ):
            group_type, reason = "top_title", "top_centered"
        elif 0.52 * height <= box_cy <= 0.95 * height and abs(center_ratio - 1.0) <= config.center_tolerance_bottom:
            group_type, reason = "bottom_subtitle", "bottom_centered"
        else:
            group_type, reason = "unknown_text", "outside_candidate_region"
        classified.append(
            TextGroup(
                frame_index=frame_index,
                shot_id=shot_id,
                bbox=bbox,
                line_bboxes=group["line_bboxes"],
                text=group["text"],
                score=group["score"],
                line_count=len(group["line_bboxes"]),
                median_line_height=median_height,
                type=group_type,
                reason=reason,
            )
        )

    selected: list[TextGroup] = []
    for half_type in ("top_title", "bottom_subtitle"):
        in_half = [group for group in classified if group.type == half_type]
        if in_half:
            selected.append(max(in_half, key=lambda group: _candidate_score(group, width, height)))
    selected_ids = {id(group) for group in selected}
    for group in classified:
        if group.type in PROCESS_TYPES and id(group) not in selected_ids:
            group.type = "unknown_text"
            group.reason = "lower_ranked_same_half"
    return classified


def _merge_same_line(items: list[dict[str, Any]], width: int, height: int) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: (((item["bbox"][1] + item["bbox"][3]) / 2), item["bbox"][0]))
    lines: list[list[dict[str, Any]]] = []
    for item in ordered:
        x1, y1, x2, y2 = item["bbox"]
        center_y = (y1 + y2) / 2
        item_height = y2 - y1
        target: list[dict[str, Any]] | None = None
        for line in reversed(lines[-4:]):
            line_bbox = _union_bbox([entry["bbox"] for entry in line])
            line_center_y = (line_bbox[1] + line_bbox[3]) / 2
            line_height = line_bbox[3] - line_bbox[1]
            horizontal_gap = max(0, x1 - line_bbox[2], line_bbox[0] - x2)
            if abs(center_y - line_center_y) <= 0.42 * max(item_height, line_height) and horizontal_gap <= 0.08 * width:
                target = line
                break
        if target is None:
            lines.append([item])
        else:
            target.append(item)
    merged: list[dict[str, Any]] = []
    for line in lines:
        line = sorted(line, key=lambda item: item["bbox"][0])
        merged.append(
            {
                "bbox": _union_bbox([item["bbox"] for item in line]),
                "line_bboxes": [_union_bbox([item["bbox"] for item in line])],
                "text": " ".join(item["text"].strip() for item in line if item["text"].strip()),
                "score": float(np.mean([item["score"] for item in line])),
                "source_items": line,
            }
        )
    return merged


def _merge_multiline(lines: list[dict[str, Any]], width: int, height: int) -> list[dict[str, Any]]:
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    groups: list[list[dict[str, Any]]] = []
    for line in ordered:
        target: list[dict[str, Any]] | None = None
        for group in reversed(groups[-4:]):
            last = group[-1]
            if _lines_belong_together(last["bbox"], line["bbox"], width, height):
                target = group
                break
        if target is None:
            groups.append([line])
        else:
            target.append(line)
    merged: list[dict[str, Any]] = []
    for group in groups:
        line_bboxes = [line["bbox"] for line in group]
        source_items = [item for line in group for item in line["source_items"]]
        merged.append(
            {
                "bbox": _union_bbox(line_bboxes),
                "line_bboxes": line_bboxes,
                "text": "\n".join(line["text"] for line in group if line["text"]),
                "score": float(np.mean([line["score"] for line in group])),
                "source_items": source_items,
            }
        )
    return merged


def _lines_belong_together(first: list[int], second: list[int], width: int, height: int) -> bool:
    f_height = first[3] - first[1]
    s_height = second[3] - second[1]
    height_ratio = f_height / max(s_height, 1)
    center_delta = abs((first[0] + first[2]) / 2 - (second[0] + second[2]) / 2)
    vertical_gap = max(0, second[1] - first[3])
    overlap = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    overlap_ratio = overlap / max(1, min(first[2] - first[0], second[2] - second[0]))
    max_gap = max(20 * height / 1920, 0.70 * np.median([f_height, s_height]))
    return (
        center_delta <= 0.05 * width
        and 0.75 <= height_ratio <= 1.33
        and vertical_gap <= max_gap
        and overlap_ratio >= 0.50
    )


def _candidate_score(group: TextGroup, width: int, height: int) -> float:
    x1, y1, x2, y2 = group.bbox
    width_ratio = (x2 - x1) / width
    height_ratio = group.median_line_height / height
    center_offset = abs((x1 + x2) / 2 - width / 2) / width
    plausible_width = 1.0 if 0.12 <= width_ratio <= 0.95 else 0.4
    return group.score + min(height_ratio / 0.04, 1.0) + plausible_width - 3.0 * center_offset


def _build_tracks(
    samples: list[Sample],
    info: VideoInfo,
    interval_frames: int,
    config: SubtitleDetectConfig,
) -> list[Track]:
    tracks: list[Track] = []
    active: dict[tuple[int, str], Track] = {}
    counters = Counter()
    for sample in samples:
        for half_type in ("top_title", "bottom_subtitle"):
            candidates = [group for group in sample.groups if group.type == half_type]
            if not candidates:
                continue
            group = max(candidates, key=lambda item: _candidate_score(item, info.width, info.height))
            key = (sample.shot_id, half_type)
            track = active.get(key)
            if track is None or not _track_matches(track, group, info):
                counters[half_type] += 1
                prefix = "top" if half_type == "top_title" else "bottom"
                track = Track(
                    track_id=f"{prefix}_{counters[half_type]:03d}",
                    type=half_type,
                    shot_id=sample.shot_id,
                )
                tracks.append(track)
                active[key] = track
            track.observations.append(group)

    confirmed: list[Track] = []
    max_gap = max(round(0.4 * info.fps), interval_frames * 2)
    for track in tracks:
        observations = sorted(track.observations, key=lambda item: item.frame_index)
        runs = _split_by_frame_gap(observations, max_gap)
        for run_index, run in enumerate(runs):
            if len(run) < 2:
                continue
            if run_index > 0:
                track = Track(
                    track_id=f"{track.track_id}_{run_index + 1}",
                    type=track.type,
                    shot_id=track.shot_id,
                )
            track.observations = run
            track.stable_bbox = _stable_bbox(run, info, config, track.type)
            track.confidence = float(np.mean([item.score for item in run]))
            confirmed.append(track)
    return confirmed


def _track_matches(track: Track, group: TextGroup, info: VideoInfo) -> bool:
    previous = track.observations[-1]
    iou = _bbox_iou(previous.bbox, group.bbox)
    center_shift = abs(_center_x(previous.bbox) - _center_x(group.bbox))
    baseline_shift = abs(previous.bbox[3] - group.bbox[3])
    height_ratio = (previous.bbox[3] - previous.bbox[1]) / max(group.bbox[3] - group.bbox[1], 1)
    return (
        (iou >= 0.50 or (center_shift <= 0.04 * info.width and baseline_shift <= 0.03 * info.height))
        and 0.75 <= height_ratio <= 1.33
    )


def _split_by_frame_gap(observations: list[TextGroup], max_gap: int) -> list[list[TextGroup]]:
    runs: list[list[TextGroup]] = []
    for observation in observations:
        if not runs or observation.frame_index - runs[-1][-1].frame_index > max_gap:
            runs.append([observation])
        else:
            runs[-1].append(observation)
    return runs


def _stable_bbox(
    observations: list[TextGroup],
    info: VideoInfo,
    config: SubtitleDetectConfig,
    track_type: str,
) -> list[int]:
    boxes = np.asarray([item.bbox for item in observations], dtype=np.float64)
    centers = np.column_stack(((boxes[:, 0] + boxes[:, 2]) / 2, (boxes[:, 1] + boxes[:, 3]) / 2))
    sizes = np.column_stack((boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]))
    features = np.column_stack((centers, sizes))
    median = np.median(features, axis=0)
    mad = np.median(np.abs(features - median), axis=0)
    scale = np.where(mad < 1.0, 1.0, mad)
    keep = np.all(np.abs(features - median) <= 2.5 * scale, axis=1)
    filtered = boxes[keep] if np.any(keep) else boxes
    x1 = int(math.floor(np.percentile(filtered[:, 0], 5)))
    y1 = int(math.floor(np.percentile(filtered[:, 1], 5)))
    x2 = int(math.ceil(np.percentile(filtered[:, 2], 95)))
    y2 = int(math.ceil(np.percentile(filtered[:, 3], 95)))
    median_text_height = float(np.median([item.median_line_height for item in observations]))
    horizontal_padding = max(1, round(config.horizontal_padding_ratio * info.width))
    vertical_padding = max(round(8 * info.height / 1920), round(0.15 * median_text_height))
    bbox = [
        max(0, x1 - horizontal_padding),
        max(0, y1 - vertical_padding),
        min(info.width - 1, x2 + horizontal_padding),
        min(info.height - 1, y2 + vertical_padding),
    ]
    if track_type == "top_title":
        bbox[3] = min(bbox[3], round(0.50 * info.height) - 1)
    else:
        bbox[1] = max(bbox[1], round(0.50 * info.height))
    return bbox


def _build_events(
    tracks: list[Track],
    info: VideoInfo,
    interval_frames: int,
    config: SubtitleDetectConfig,
    asr_segments: list[AsrSegment],
) -> list[Event]:
    events: list[Event] = []
    counters = Counter()
    for track in tracks:
        chunks = _split_track_events(track.observations)
        for observations in chunks:
            if len(observations) < 2:
                continue
            counters[track.type] += 1
            prefix = "title" if track.type == "top_title" else "subtitle"
            source_text = _vote_text(observations)
            start_frame = observations[0].frame_index
            end_frame = min(info.frame_count - 1, observations[-1].frame_index + interval_frames - 1)
            stable_bbox = _stable_bbox(observations, info, config, track.type)
            duration = (end_frame - start_frame + 1) / info.fps
            event_type = track.type
            if track.type == "bottom_subtitle" and duration > 4.5:
                event_type = "cta_text"
            asr_overlap = _asr_overlap(start_frame, end_frame, info.fps, asr_segments)
            needs_review = not bool(source_text.strip())
            if asr_overlap is not None and event_type == "bottom_subtitle" and asr_overlap < 0.15:
                needs_review = True
            events.append(
                Event(
                    event_id=f"{prefix}_{counters[track.type]:04d}",
                    track_id=track.track_id,
                    type=event_type,
                    shot_id=track.shot_id,
                    observations=observations,
                    start_frame=start_frame,
                    end_frame=end_frame,
                    source_text=source_text,
                    line_count=Counter(item.line_count for item in observations).most_common(1)[0][0],
                    line_bboxes=_representative_line_bboxes(observations),
                    stable_bbox=stable_bbox,
                    ocr_confidence=float(np.mean([item.score for item in observations])),
                    asr_overlap=asr_overlap,
                    needs_review=needs_review,
                )
            )
    return sorted(events, key=lambda event: (event.start_frame, event.type))


def _consolidate_events(
    events: list[Event],
    info: VideoInfo,
    config: SubtitleDetectConfig,
) -> list[Event]:
    consolidated: list[Event] = []
    groups: dict[tuple[int, str], list[Event]] = {}
    for event in events:
        half = "top" if _center_y(event.stable_bbox) < 0.5 * info.height else "bottom"
        groups.setdefault((event.shot_id, half), []).append(event)
    for group_events in groups.values():
        ordered = sorted(group_events, key=lambda event: (event.start_frame, event.end_frame))
        merged: list[Event] = []
        for event in ordered:
            if not merged:
                merged.append(event)
                continue
            previous = merged[-1]
            same_visual_event = (
                _bbox_iou(previous.stable_bbox, event.stable_bbox) >= 0.45
                and text_similarity(previous.source_text, event.source_text) >= 0.72
                and event.start_frame <= previous.end_frame + round(0.4 * info.fps)
            )
            if same_visual_event:
                observations = sorted(
                    previous.observations + event.observations,
                    key=lambda observation: observation.frame_index,
                )
                previous.observations = observations
                previous.end_frame = max(previous.end_frame, event.end_frame)
                previous.source_text = _vote_text(observations)
                previous.line_count = Counter(item.line_count for item in observations).most_common(1)[0][0]
                previous.line_bboxes = _representative_line_bboxes(observations)
                previous.stable_bbox = _stable_bbox(observations, info, config, previous.type)
                previous.ocr_confidence = float(np.mean([item.score for item in observations]))
                previous.needs_review = previous.needs_review or event.needs_review
                continue
            if event.start_frame <= previous.end_frame:
                previous.end_frame = event.start_frame - 1
                if previous.end_frame < previous.start_frame:
                    if event.ocr_confidence > previous.ocr_confidence:
                        merged[-1] = event
                    continue
            merged.append(event)
        consolidated.extend(event for event in merged if event.end_frame >= event.start_frame)
    return sorted(consolidated, key=lambda event: (event.start_frame, event.type))


def _split_track_events(observations: list[TextGroup]) -> list[list[TextGroup]]:
    if not observations:
        return []
    chunks: list[list[TextGroup]] = [[observations[0]]]
    pending: list[TextGroup] = []
    for observation in observations[1:]:
        reference = chunks[-1][-1]
        text_changed = text_similarity(reference.text, observation.text) < 0.55
        line_changed = reference.line_count != observation.line_count
        height_change = abs((reference.bbox[3] - reference.bbox[1]) - (observation.bbox[3] - observation.bbox[1])) / max(
            reference.bbox[3] - reference.bbox[1], 1
        ) > 0.35
        if text_changed or line_changed or height_change:
            pending.append(observation)
            if len(pending) >= 2:
                chunks.append(pending.copy())
                pending.clear()
        else:
            if pending:
                chunks[-1].extend(pending)
                pending.clear()
            chunks[-1].append(observation)
    if pending:
        chunks[-1].extend(pending)
    return chunks


def normalize_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def text_similarity(first: str, second: str) -> float:
    first_normalized = normalize_text(first)
    second_normalized = normalize_text(second)
    if not first_normalized and not second_normalized:
        return 1.0
    return SequenceMatcher(None, first_normalized, second_normalized).ratio()


def _vote_text(observations: list[TextGroup]) -> str:
    candidates: list[tuple[str, float]] = []
    for observation in observations:
        text = observation.text.strip()
        if not text:
            continue
        weight = observation.score
        for existing_text, existing_weight in candidates:
            if text_similarity(text, existing_text) >= 0.75:
                weight += 0.25 * existing_weight
        candidates.append((text, weight))
    return max(candidates, key=lambda item: item[1])[0] if candidates else ""


def _representative_line_bboxes(observations: list[TextGroup]) -> list[list[int]]:
    representative = max(observations, key=lambda item: (item.score, len(normalize_text(item.text))))
    return representative.line_bboxes


def _asr_overlap(
    start_frame: int,
    end_frame: int,
    fps: float,
    segments: list[AsrSegment],
) -> float | None:
    if not segments:
        return None
    start = start_frame / fps
    end = (end_frame + 1) / fps
    duration = max(end - start, 1e-6)
    overlap = 0.0
    for segment in segments:
        overlap += max(0.0, min(end, segment.end) - max(start, segment.start))
    return min(1.0, overlap / duration)


def _refine_event_boundaries(
    engine: PaddleOcrEngine,
    info: VideoInfo,
    events: list[Event],
    temp_dir: Path,
    source_lang_hint: str | None,
    interval_frames: int,
    refine_step: int,
    min_score: float,
) -> None:
    if not events:
        return
    requested: dict[tuple[int, tuple[int, int, int, int]], list[Event]] = {}
    for event in events:
        bbox_key = tuple(event.stable_bbox)
        first_observation = event.observations[0].frame_index
        last_observation = event.observations[-1].frame_index
        for frame_index in range(
            max(0, first_observation - interval_frames),
            first_observation + 1,
            refine_step,
        ):
            requested.setdefault((frame_index, bbox_key), []).append(event)
        for frame_index in range(
            last_observation,
            min(info.frame_count, last_observation + interval_frames + 1),
            refine_step,
        ):
            requested.setdefault((frame_index, bbox_key), []).append(event)

    capture = cv2.VideoCapture(str(info.path))
    presence: dict[tuple[str, int], bool] = {}
    request_items = sorted(requested)
    for batch_start in range(0, len(request_items), 32):
        inputs: list[ImageInput] = []
        mapping: dict[str, tuple[int, tuple[int, int, int, int]]] = {}
        for sequence, (frame_index, bbox_key) in enumerate(request_items[batch_start : batch_start + 32]):
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                continue
            image_path = temp_dir / f"refine_{batch_start + sequence:06d}.jpg"
            if not cv2.imwrite(str(image_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 94]):
                continue
            image_id = str(batch_start + sequence)
            inputs.append(
                ImageInput(
                    image_id=image_id,
                    path=str(image_path),
                    time=frame_index / info.fps,
                    regions=[Region(name="event", bbox=list(bbox_key))],
                )
            )
            mapping[image_id] = (frame_index, bbox_key)
        if not inputs:
            continue
        for result in engine.recognize_batch(inputs):
            frame_index, bbox_key = mapping[result.image_id]
            text = " ".join(item.text for item in result.items if item.score >= min_score)
            for event in requested[(frame_index, bbox_key)]:
                matches = bool(text.strip()) and text_similarity(text, event.source_text) >= 0.30
                presence[(event.event_id, frame_index)] = matches
    capture.release()

    for event in events:
        start_candidates = [
            frame for (event_id, frame), present in presence.items()
            if event_id == event.event_id and present and frame <= event.observations[0].frame_index
        ]
        end_candidates = [
            frame for (event_id, frame), present in presence.items()
            if event_id == event.event_id and present and frame >= event.observations[-1].frame_index
        ]
        if start_candidates:
            event.start_frame = min(start_candidates)
        else:
            event.needs_review = True
        if end_candidates:
            event.end_frame = max(end_candidates)
        else:
            event.needs_review = True


def _attach_color_blocks(info: VideoInfo, events: list[Event]) -> None:
    capture = cv2.VideoCapture(str(info.path))
    for event in events:
        frame_index = event.observations[len(event.observations) // 2].frame_index
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if ok:
            event.color_block = detect_color_block(frame, event.stable_bbox)
    capture.release()


def detect_color_block(frame: np.ndarray, text_bbox: list[int]) -> dict[str, Any] | None:
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = text_bbox
    text_width = x2 - x1
    text_height = y2 - y1
    rx1 = max(0, x1 - round(0.18 * text_width))
    ry1 = max(0, y1 - round(0.55 * text_height))
    rx2 = min(width, x2 + round(0.18 * text_width))
    ry2 = min(height, y2 + round(0.55 * text_height))
    roi = frame[ry1:ry2, rx1:rx2]
    if roi.size == 0:
        return None
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
    ring_mask = np.ones(roi.shape[:2], dtype=np.uint8)
    local_text = [
        max(0, x1 - rx1),
        max(0, y1 - ry1),
        min(roi.shape[1], x2 - rx1),
        min(roi.shape[0], y2 - ry1),
    ]
    ring_mask[local_text[1]:local_text[3], local_text[0]:local_text[2]] = 0
    ring_pixels = lab[ring_mask > 0]
    if len(ring_pixels) < 20:
        return None
    quantized = (ring_pixels // 12).astype(np.int16)
    colors, counts = np.unique(quantized, axis=0, return_counts=True)
    dominant_lab = colors[int(np.argmax(counts))] * 12 + 6
    distance = np.linalg.norm(lab.astype(np.float32) - dominant_lab.astype(np.float32), axis=2)
    mask = (distance <= 24).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best: dict[str, Any] | None = None
    text_center = np.array([(local_text[0] + local_text[2]) / 2, (local_text[1] + local_text[3]) / 2])
    text_area = max(1, text_width * text_height)
    for contour in contours:
        bx, by, bw, bh = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        rect_area = max(1, bw * bh)
        if rect_area < text_area or area / rect_area < 0.72:
            continue
        block_center = np.array([bx + bw / 2, by + bh / 2])
        center_ratio = np.abs(text_center - block_center) / np.array([max(bw, 1), max(bh, 1)])
        text_ratio = text_area / rect_area
        if np.any(center_ratio > 0.12) or not 0.15 <= text_ratio <= 0.80:
            continue
        corner_patch = max(2, round(min(bw, bh) * 0.08))
        corners = [
            mask[by:by + corner_patch, bx:bx + corner_patch],
            mask[by:by + corner_patch, bx + bw - corner_patch:bx + bw],
            mask[by + bh - corner_patch:by + bh, bx:bx + corner_patch],
            mask[by + bh - corner_patch:by + bh, bx + bw - corner_patch:bx + bw],
        ]
        corner_fill = float(np.mean([np.mean(patch > 0) if patch.size else 1.0 for patch in corners]))
        shape = "rounded_rectangle" if corner_fill < 0.65 else "rectangle"
        candidate = {
            "bbox": [rx1 + bx, ry1 + by, rx1 + bx + bw, ry1 + by + bh],
            "shape": shape,
            "corner_radius": round(min(bw, bh) * (0.10 if shape == "rounded_rectangle" else 0.0)),
            "fill_color_bgr": [int(value) for value in np.median(roi[mask > 0], axis=0)],
            "confidence": round(float(area / rect_area), 4),
        }
        if best is None or candidate["confidence"] > best["confidence"]:
            best = candidate
    return best


def _collect_ignored_regions(samples: list[Sample]) -> list[dict[str, Any]]:
    ignored: list[dict[str, Any]] = []
    seen: list[list[int]] = []
    for sample in samples:
        for group in sample.groups:
            if group.type not in {"product_text", "logo_watermark"}:
                continue
            if any(_bbox_iou(group.bbox, bbox) >= 0.65 for bbox in seen):
                continue
            seen.append(group.bbox)
            ignored.append(
                {
                    "type": group.type,
                    "bbox": group.bbox,
                    "reason": group.reason,
                    "sample_frame": sample.frame_index,
                }
            )
    return ignored


def _build_qa(
    info: VideoInfo,
    events: list[Event],
    ignored_regions: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    failures: list[str] = []
    warnings: list[str] = []
    max_boxes = 0
    for frame_index in range(info.frame_count):
        active = [event for event in events if event.start_frame <= frame_index <= event.end_frame]
        max_boxes = max(max_boxes, len(active))
    if max_boxes > 2:
        failures.append(f"max_boxes_per_frame={max_boxes}")
    empty_events = [event.event_id for event in events if not event.source_text.strip()]
    if empty_events:
        failures.append(f"empty_text_events={','.join(empty_events)}")
    oversized = [
        event.event_id
        for event in events
        if _bbox_area(event.stable_bbox) / (info.width * info.height) > 0.25
    ]
    if oversized:
        failures.append(f"oversized_events={','.join(oversized)}")
    overlap_events: list[str] = []
    for event in events:
        if any(_bbox_iou(event.stable_bbox, region["bbox"]) > 0.30 for region in ignored_regions):
            overlap_events.append(event.event_id)
    if overlap_events:
        warnings.append(f"protected_overlap={','.join(overlap_events)}")
    if any(event.needs_review for event in events):
        warnings.append("event_boundary_or_asr_needs_review")
    coverage_values: list[float] = []
    for event in events:
        for observation in event.observations:
            coverage_values.append(_coverage(observation.bbox, event.stable_bbox))
    min_coverage = min(coverage_values, default=1.0)
    mean_coverage = float(np.mean(coverage_values)) if coverage_values else 1.0
    if min_coverage < 0.99:
        warnings.append(f"min_observed_text_coverage={min_coverage:.4f}")
    status = "failed" if failures else ("needs_review" if warnings else "succeeded")
    return status, {
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "metrics": {
            "events": len(events),
            "max_boxes_per_frame": max_boxes,
            "empty_box_rate": 0.0 if not events else len(empty_events) / len(events),
            "mean_observed_text_coverage": round(mean_coverage, 5),
            "min_observed_text_coverage": round(min_coverage, 5),
            "oversized_event_count": len(oversized),
            "protected_overlap_event_count": len(overlap_events),
        },
        "limitations": [
            "Precision and recall require human ground-truth annotations; this report only enforces structural gates.",
            "ASR overlap is omitted when asr_segments are not supplied.",
        ],
    }


def _render_debug_outputs(
    info: VideoInfo,
    samples: list[Sample],
    events: list[Event],
    output_dir: Path,
) -> dict[str, str | None]:
    output_names = {
        "raw_ocr": output_dir / "raw_ocr.mp4",
        "classified_tracks": output_dir / "classified_tracks.mp4",
        "white_mask": output_dir / "white_mask.mp4",
        "stable_bbox": output_dir / "stable_bbox.mp4",
    }
    temp_paths = {key: output_dir / f".{key}_silent.mp4" for key in output_names}
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writers = {
        key: cv2.VideoWriter(str(path), fourcc, info.fps, (info.width, info.height))
        for key, path in temp_paths.items()
    }
    if not all(writer.isOpened() for writer in writers.values()):
        for writer in writers.values():
            writer.release()
        raise RuntimeError("failed to create debug video writers")

    sample_indices = [sample.frame_index for sample in samples]
    capture = cv2.VideoCapture(str(info.path))
    for frame_index in range(info.frame_count):
        ok, frame = capture.read()
        if not ok:
            break
        sample = samples[_nearest_index(sample_indices, frame_index)]
        raw_frame = frame.copy()
        for item in sample.raw_items:
            _draw_box(raw_frame, item["bbox"], (0, 255, 255), item.get("text", ""), 2)
        writers["raw_ocr"].write(raw_frame)

        classified = frame.copy()
        for group in sample.groups:
            _draw_box(classified, group.bbox, TYPE_COLORS[group.type], group.type, 3)
        writers["classified_tracks"].write(classified)

        active = [event for event in events if event.start_frame <= frame_index <= event.end_frame]
        white = frame.copy()
        stable = frame.copy()
        for event in active[:2]:
            x1, y1, x2, y2 = event.stable_bbox
            cv2.rectangle(white, (x1, y1), (x2, y2), (255, 255, 255), thickness=-1)
            label = f"{event.event_id} {event.start_frame}-{event.end_frame}"
            _draw_box(stable, event.stable_bbox, TYPE_COLORS[event.type], label, 3)
        writers["white_mask"].write(white)
        writers["stable_bbox"].write(stable)
    capture.release()
    for writer in writers.values():
        writer.release()

    outputs: dict[str, str | None] = {}
    for key, destination in output_names.items():
        _mux_audio(info.path, temp_paths[key], destination)
        temp_paths[key].unlink(missing_ok=True)
        outputs[key] = str(destination)
    return outputs


def _draw_box(frame: np.ndarray, bbox: list[int], color: tuple[int, int, int], label: str, thickness: int) -> None:
    x1, y1, x2, y2 = bbox
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)
    if label:
        cv2.putText(
            frame,
            label[:80],
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )


def _mux_audio(source: Path, silent: Path, output: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        silent.replace(output)
        return
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(silent),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_review_html(source: Path, output_dir: Path, outputs: dict[str, str | None]) -> Path:
    review_path = output_dir / "review.html"
    source_preview = output_dir / f"source{source.suffix.lower()}"
    if not source_preview.exists():
        try:
            os.link(source, source_preview)
        except OSError:
            shutil.copy2(source, source_preview)
    cards = [
        f'<section><h2>Source</h2><video controls preload="metadata" src="{html.escape(source_preview.name)}"></video></section>'
    ]
    for title, key in [
        ("Raw OCR", "raw_ocr"),
        ("Classified tracks", "classified_tracks"),
        ("White mask", "white_mask"),
        ("Stable bbox", "stable_bbox"),
    ]:
        path = outputs.get(key)
        if path:
            cards.append(
                f'<section><h2>{html.escape(title)}</h2><video controls preload="metadata" src="{html.escape(Path(path).name)}"></video></section>'
            )
    review_path.write_text(
        """<!doctype html><html><head><meta charset="utf-8"><title>Subtitle erasure review</title>
<style>body{font-family:system-ui;background:#0b0e14;color:#e9eef7;margin:24px}main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}section{background:#151b26;padding:14px;border-radius:12px}video{width:100%;max-height:76vh;background:#000}h1,h2{margin:.2em 0 .6em}a{color:#78b7ff}</style></head><body>"""
        + f"<h1>精准字幕擦除复核</h1><p>原视频：{html.escape(str(source))} · <a href=\"subtitle_events.json\">JSON</a> · <a href=\"qa_report.json\">QA</a></p><main>"
        + "".join(cards)
        + """</main><script>
const videos=[...document.querySelectorAll('video')];
let syncing=false;
for(const video of videos){
  video.addEventListener('play',()=>{if(syncing)return;syncing=true;for(const other of videos){if(other!==video){other.currentTime=video.currentTime;other.play();}}syncing=false;});
  video.addEventListener('pause',()=>{if(syncing)return;syncing=true;for(const other of videos){if(other!==video)other.pause();}syncing=false;});
  video.addEventListener('seeking',()=>{if(syncing)return;syncing=true;for(const other of videos){if(other!==video)other.currentTime=video.currentTime;}syncing=false;});
}
</script></body></html>""",
        encoding="utf-8",
    )
    return review_path


def _event_payload(event: Event, info: VideoInfo) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "track_id": event.track_id,
        "type": event.type,
        "shot_id": event.shot_id,
        "start_frame": event.start_frame,
        "end_frame": event.end_frame,
        "start_time": round(event.start_frame / info.fps, 6),
        "end_time": round(event.end_frame / info.fps, 6),
        "source_text": event.source_text,
        "line_count": event.line_count,
        "line_bboxes": event.line_bboxes,
        "stable_bbox": event.stable_bbox,
        "ocr_confidence": round(event.ocr_confidence, 5),
        "asr_overlap": None if event.asr_overlap is None else round(event.asr_overlap, 5),
        "color_block": event.color_block,
        "needs_review": event.needs_review,
    }


def _track_payload(track: Track) -> dict[str, Any]:
    return {
        "track_id": track.track_id,
        "type": track.type,
        "shot_id": track.shot_id,
        "stable_bbox": track.stable_bbox,
        "confidence": round(track.confidence, 5),
        "observation_count": len(track.observations),
    }


def _nearest_index(sorted_values: list[int], target: int) -> int:
    if not sorted_values:
        return 0
    position = int(np.searchsorted(sorted_values, target))
    if position <= 0:
        return 0
    if position >= len(sorted_values):
        return len(sorted_values) - 1
    before = sorted_values[position - 1]
    after = sorted_values[position]
    return position - 1 if target - before <= after - target else position


def _union_bbox(boxes: Iterable[list[int]]) -> list[int]:
    boxes = list(boxes)
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _clamp_bbox(bbox: list[int], width: int, height: int) -> list[int]:
    x1, y1, x2, y2 = [int(round(value)) for value in bbox]
    return [
        max(0, min(width - 1, x1)),
        max(0, min(height - 1, y1)),
        max(0, min(width - 1, x2)),
        max(0, min(height - 1, y2)),
    ]


def _bbox_iou(first: list[int], second: list[int]) -> float:
    intersection = _intersection_area(first, second)
    union = _bbox_area(first) + _bbox_area(second) - intersection
    return intersection / union if union > 0 else 0.0


def _coverage(subject: list[int], cover: list[int]) -> float:
    area = _bbox_area(subject)
    return _intersection_area(subject, cover) / area if area > 0 else 0.0


def _intersection_area(first: list[int], second: list[int]) -> int:
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _bbox_area(bbox: list[int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def _center_x(bbox: list[int]) -> float:
    return (bbox[0] + bbox[2]) / 2


def _center_y(bbox: list[int]) -> float:
    return (bbox[1] + bbox[3]) / 2
