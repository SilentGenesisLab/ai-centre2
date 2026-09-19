from __future__ import annotations

import gc
import importlib
import sys
import time
from pathlib import Path
from typing import Callable, Protocol

import cv2
import imageio.v2 as imageio
import numpy as np
import torch
from decord import VideoReader, cpu


ProgressCallback = Callable[[str, int], None]


class VideoDepthInference(Protocol):
    version: str
    model_size: str
    model_name: str

    def process(
        self,
        source: Path,
        target: Path,
        *,
        input_size: int,
        max_resolution: int,
        target_fps: float,
        progress: ProgressCallback,
    ) -> dict[str, float | int]: ...

    def close(self) -> None: ...


def _scaled_reader(
    source: Path,
    target_fps: float,
    max_resolution: int,
) -> tuple[VideoReader, list[int], float]:
    probe = VideoReader(str(source), ctx=cpu(0))
    if len(probe) == 0:
        raise RuntimeError("input video contains no decodable frames")
    height, width = probe[0].shape[:2]
    if max(height, width) > max_resolution:
        scale = max_resolution / max(height, width)
        height = max(2, round(height * scale))
        width = max(2, round(width * scale))
        height += height % 2
        width += width % 2
    reader = VideoReader(str(source), ctx=cpu(0), width=width, height=height)
    original_fps = float(reader.get_avg_fps())
    if not np.isfinite(original_fps) or original_fps <= 0:
        raise RuntimeError("input video has an invalid frame rate")
    output_fps = original_fps if target_fps == -1 else min(target_fps, original_fps)
    stride = max(round(original_fps / output_fps), 1)
    indices = list(range(0, len(reader), stride))
    return reader, indices, original_fps / stride


def _writer(target: Path, fps: float):
    target.parent.mkdir(parents=True, exist_ok=True)
    return imageio.get_writer(
        str(target),
        fps=fps,
        macro_block_size=1,
        codec="libx264",
        ffmpeg_params=["-crf", "12", "-pix_fmt", "yuv420p"],
    )


def _scene_ranges(source: Path, frame_count: int, output_fps: float) -> list[tuple[int, int]]:
    """Detect hard cuts and map source timestamps to sampled output frame ranges."""
    try:
        from scenedetect import ContentDetector, detect

        scenes = detect(
            str(source),
            ContentDetector(threshold=27.0, min_scene_len=15),
            show_progress=False,
        )
        cuts = {
            max(1, min(frame_count - 1, round(end.get_seconds() * output_fps)))
            for _, end in scenes[:-1]
            if frame_count > 1
        }
    except Exception:
        cuts = set()
    boundaries = [0, *sorted(cuts), frame_count]
    return [(start, end) for start, end in zip(boundaries[:-1], boundaries[1:]) if end > start]


class VideoDepthAnythingInference:
    """Video Depth Anything inference using its DA2 Small or Base backbone."""

    version = "da2"
    _CONFIGS = {
        "small": ("vits", 64, [48, 96, 192, 384]),
        "base": ("vitb", 128, [96, 192, 384, 768]),
    }

    def __init__(
        self,
        source_dir: Path,
        checkpoint: Path,
        model_size: str,
        device: str = "cuda",
    ) -> None:
        if model_size not in self._CONFIGS:
            raise ValueError(f"unsupported DA2 model size: {model_size}")
        if not source_dir.is_dir():
            raise RuntimeError(f"Video Depth Anything inference source is missing: {source_dir}")
        if not checkpoint.is_file():
            raise RuntimeError(f"Video Depth Anything checkpoint is missing: {checkpoint}")
        source_path = str(source_dir)
        if source_path not in sys.path:
            sys.path.insert(0, source_path)
        module = importlib.import_module("video_depth_anything.video_depth")
        encoder, features, out_channels = self._CONFIGS[model_size]
        network = module.VideoDepthAnything(
            encoder=encoder,
            features=features,
            out_channels=out_channels,
        )
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        network.load_state_dict(state, strict=True)
        self.model = network.to(device).eval()
        self.model_size = model_size
        self.model_name = f"Video-Depth-Anything-{model_size.title()}"
        self.device = device

    def close(self) -> None:
        if hasattr(self, "model"):
            self.model.to("cpu")
            del self.model
        gc.collect()
        torch.cuda.empty_cache()

    def process(
        self,
        source: Path,
        target: Path,
        *,
        input_size: int,
        max_resolution: int,
        target_fps: float,
        progress: ProgressCallback,
    ) -> dict[str, float | int]:
        started = time.perf_counter()
        reader, indices, output_fps = _scaled_reader(source, target_fps, max_resolution)
        frames = reader.get_batch(indices).asnumpy()
        del reader
        scene_ranges = _scene_ranges(source, len(frames), output_fps)
        progress("inferring", 20)
        torch.cuda.reset_peak_memory_stats()
        inference_started = time.perf_counter()
        try:
            shot_depths = []
            for shot_index, (shot_start, shot_end) in enumerate(scene_ranges, 1):
                current, output_fps = self.model.infer_video_depth(
                    frames[shot_start:shot_end],
                    output_fps,
                    input_size=input_size,
                    device=self.device,
                    fp32=False,
                )
                shot_depths.append(current)
                progress("inferring", 20 + round(55 * shot_index / len(scene_ranges)))
            depths = np.concatenate(shot_depths, axis=0)
            inference_seconds = time.perf_counter() - inference_started
            progress("encoding", 80)
            self._write_depth_video(target, depths, output_fps, scene_ranges)
            return {
                "frame_count": int(len(depths)),
                "fps": round(float(output_fps), 3),
                "duration_seconds": round(float(len(depths) / output_fps), 3),
                "output_width": int(depths.shape[2]),
                "output_height": int(depths.shape[1]),
                "scene_count": len(scene_ranges),
                "inference_seconds": round(inference_seconds, 3),
                "processing_seconds": round(time.perf_counter() - started, 3),
                "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 3),
            }
        finally:
            if "depths" in locals():
                del depths
            del frames
            gc.collect()
            torch.cuda.empty_cache()

    @staticmethod
    def _write_depth_video(
        target: Path,
        depths: np.ndarray,
        fps: float,
        scene_ranges: list[tuple[int, int]],
    ) -> None:
        writer = _writer(target, fps)
        try:
            for scene_start, scene_end in scene_ranges:
                sampled = depths[scene_start:scene_end, ::4, ::4]
                finite = sampled[np.isfinite(sampled)]
                if finite.size == 0:
                    raise RuntimeError("depth output contains no finite values")
                low, high = np.percentile(finite, [2, 98])
                for depth in depths[scene_start:scene_end]:
                    gray = np.clip((depth - low) / max(high - low, 1e-6), 0, 1)
                    frame = (gray * 255).astype(np.uint8)
                    writer.append_data(np.repeat(frame[..., None], 3, axis=2))
        finally:
            writer.close()


class DepthAnything3Inference:
    """Chunked DA3 video inference with disk-backed CPU depth storage."""

    version = "da3"

    def __init__(
        self,
        source_dir: Path,
        model_dir: Path,
        model_size: str,
        *,
        chunk_size: int = 12,
        overlap: int = 4,
        device: str = "cuda",
    ) -> None:
        if model_size not in {"small", "base"}:
            raise ValueError(f"unsupported DA3 model size: {model_size}")
        if not source_dir.is_dir():
            raise RuntimeError(f"Depth Anything 3 inference source is missing: {source_dir}")
        if not (model_dir / "config.json").is_file() or not (
            model_dir / "model.safetensors"
        ).is_file():
            raise RuntimeError(f"Depth Anything 3 model is incomplete: {model_dir}")
        if not 0 <= overlap < chunk_size:
            raise ValueError("DA3 overlap must be smaller than chunk size")
        source_path = str(source_dir)
        if source_path not in sys.path:
            sys.path.insert(0, source_path)
        module = importlib.import_module("depth_anything_3.api")
        network = module.DepthAnything3.from_pretrained(str(model_dir))
        self.model = network.to(device).eval()
        self.model_size = model_size
        self.model_name = f"Depth-Anything-3-{model_size.title()}"
        self.chunk_size = chunk_size
        self.overlap = overlap

    def close(self) -> None:
        if hasattr(self, "model"):
            self.model.to("cpu")
            del self.model
        gc.collect()
        torch.cuda.empty_cache()

    @staticmethod
    def _chunk_starts(total: int, size: int, overlap: int) -> list[int]:
        starts = [0]
        while starts[-1] + size < total:
            next_start = starts[-1] + size - overlap
            if next_start <= starts[-1]:
                break
            starts.append(next_start)
        return starts

    @staticmethod
    def _align_depth(current: np.ndarray, reference: np.ndarray) -> np.ndarray:
        count = len(reference)
        x = current[:count, ::4, ::4].reshape(-1).astype(np.float64)
        y = reference[:, ::4, ::4].reshape(-1).astype(np.float64)
        valid = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
        x, y = x[valid], y[valid]
        if x.size < 100:
            return current
        x_low, x_high = np.percentile(x, [2, 98])
        y_low, y_high = np.percentile(y, [2, 98])
        keep = (x >= x_low) & (x <= x_high) & (y >= y_low) & (y <= y_high)
        x, y = x[keep], y[keep]
        matrix = np.stack([x, np.ones_like(x)], axis=1)
        scale, shift = np.linalg.lstsq(matrix, y, rcond=None)[0]
        if not np.isfinite(scale) or scale <= 0 or scale > 10:
            scale = np.median(y) / max(np.median(x), 1e-6)
            shift = 0.0
        return np.maximum(current * np.float32(scale) + np.float32(shift), 1e-6)

    def process(
        self,
        source: Path,
        target: Path,
        *,
        input_size: int,
        max_resolution: int,
        target_fps: float,
        progress: ProgressCallback,
    ) -> dict[str, float | int]:
        started = time.perf_counter()
        reader, indices, output_fps = _scaled_reader(source, target_fps, max_resolution)
        total = len(indices)
        if total == 0:
            raise RuntimeError("input video contains no decodable frames")
        output_height, output_width = reader[0].shape[:2]
        scene_ranges = _scene_ranges(source, total, output_fps)
        cache_path = target.with_suffix(".depth-cache.npy")
        depth_cache: np.memmap | None = None
        covered_end = 0
        inference_seconds = 0.0
        torch.cuda.reset_peak_memory_stats()
        try:
            for scene_start, scene_end in scene_ranges:
                covered_end = scene_start
                relative_starts = self._chunk_starts(
                    scene_end - scene_start,
                    self.chunk_size,
                    self.overlap,
                )
                for relative_start in relative_starts:
                    chunk_start = scene_start + relative_start
                    chunk_end = min(chunk_start + self.chunk_size, scene_end)
                    frames = reader.get_batch(indices[chunk_start:chunk_end]).asnumpy()
                    tick = time.perf_counter()
                    prediction = self.model.inference(
                        list(frames),
                        process_res=input_size,
                        process_res_method="upper_bound_resize",
                        ref_view_strategy="middle",
                    )
                    inference_seconds += time.perf_counter() - tick
                    current = prediction.depth.astype(np.float32)
                    if depth_cache is None:
                        depth_cache = np.lib.format.open_memmap(
                            cache_path,
                            mode="w+",
                            dtype=np.float16,
                            shape=(total, current.shape[1], current.shape[2]),
                        )
                    actual_overlap = max(0, covered_end - chunk_start)
                    if actual_overlap:
                        reference = np.asarray(
                            depth_cache[chunk_start : chunk_start + actual_overlap],
                            dtype=np.float32,
                        )
                        current = self._align_depth(current, reference)
                        for offset in range(actual_overlap):
                            weight = (offset + 1) / (actual_overlap + 1)
                            blended = (
                                reference[offset] * (1.0 - weight) + current[offset] * weight
                            )
                            depth_cache[chunk_start + offset] = blended.astype(np.float16)
                    if actual_overlap < len(current):
                        depth_cache[chunk_start + actual_overlap : chunk_end] = current[
                            actual_overlap:
                        ].astype(np.float16)
                    covered_end = max(covered_end, chunk_end)
                    progress("inferring", 20 + round(55 * covered_end / total))
                    del frames, prediction, current
                    torch.cuda.empty_cache()
            if depth_cache is None or covered_end != total:
                raise RuntimeError("DA3 did not produce all requested frames")
            depth_cache.flush()
            progress("encoding", 80)
            self._write_depth_video(
                target,
                depth_cache,
                output_fps,
                output_width,
                output_height,
                scene_ranges,
            )
            return {
                "frame_count": total,
                "fps": round(float(output_fps), 3),
                "duration_seconds": round(float(total / output_fps), 3),
                "output_width": int(output_width),
                "output_height": int(output_height),
                "scene_count": len(scene_ranges),
                "inference_seconds": round(inference_seconds, 3),
                "processing_seconds": round(time.perf_counter() - started, 3),
                "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 3),
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.overlap,
            }
        finally:
            del reader
            if depth_cache is not None:
                del depth_cache
            cache_path.unlink(missing_ok=True)
            gc.collect()
            torch.cuda.empty_cache()

    @staticmethod
    def _write_depth_video(
        target: Path,
        depths: np.memmap,
        fps: float,
        output_width: int,
        output_height: int,
        scene_ranges: list[tuple[int, int]],
    ) -> None:
        writer = _writer(target, fps)
        try:
            for scene_start, scene_end in scene_ranges:
                sampled = np.asarray(
                    depths[scene_start:scene_end, ::4, ::4],
                    dtype=np.float32,
                )
                valid = np.isfinite(sampled) & (sampled > 0)
                if not valid.any():
                    raise RuntimeError("DA3 depth output contains no finite positive values")
                epsilon = max(float(np.percentile(sampled[valid], 1)) * 0.1, 1e-6)
                inverse_sampled = 1.0 / np.maximum(sampled[valid], epsilon)
                low, high = np.percentile(inverse_sampled, [2, 98])
                for depth in depths[scene_start:scene_end]:
                    inverse = 1.0 / np.maximum(np.asarray(depth, dtype=np.float32), epsilon)
                    gray = np.clip((inverse - low) / max(high - low, 1e-6), 0, 1)
                    frame = (gray * 255).astype(np.uint8)
                    if frame.shape != (output_height, output_width):
                        frame = cv2.resize(
                            frame,
                            (output_width, output_height),
                            interpolation=cv2.INTER_CUBIC,
                        )
                    writer.append_data(np.repeat(frame[..., None], 3, axis=2))
        finally:
            writer.close()


# Backward-compatible import name.
SmallVideoDepthInference = VideoDepthAnythingInference
