from __future__ import annotations

# This implementation is intentionally owned by this service. The AI video kernel
# calls the service over HTTP and does not import or execute face detection locally.

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np


FALLBACK_IMAGE_SIZE = 640


def _resolve_dim(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and value > 0 else fallback


def mosaic_faces(frame: np.ndarray, boxes: list[tuple[int, int, int, int]], block_size: int) -> np.ndarray:
    block = max(2, int(block_size))
    for x1, y1, x2, y2 in boxes:
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            continue
        height, width = roi.shape[:2]
        tiny = cv2.resize(roi, (max(1, width // block), max(1, height // block)), interpolation=cv2.INTER_LINEAR)
        frame[y1:y2, x1:x2] = cv2.resize(tiny, (width, height), interpolation=cv2.INTER_NEAREST)
    return frame


class FaceDetector:
    def __init__(self, model_path: Path, *, confidence_threshold: float, iou_threshold: float, intra_op_threads: int) -> None:
        import onnxruntime as ort

        if not Path(model_path).is_file():
            raise FileNotFoundError(f"FACE_MODEL_PATH not found: {model_path}")
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
        ort.set_default_logger_severity(3)
        available = ort.get_available_providers()
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if "CUDAExecutionProvider" in available else ["CPUExecutionProvider"]
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = max(1, int(intra_op_threads))
        self.session = ort.InferenceSession(str(model_path), sess_options=options, providers=providers)
        model_input = self.session.get_inputs()[0]
        model_output = self.session.get_outputs()[0]
        self.input_name = model_input.name
        self.model_height = _resolve_dim(model_input.shape[2], FALLBACK_IMAGE_SIZE)
        self.model_width = _resolve_dim(model_input.shape[3], FALLBACK_IMAGE_SIZE)
        output_dims = [_resolve_dim(value, 0) for value in model_output.shape[1:]]
        self.transpose_output = output_dims[0] == 0 or output_dims[1] == 0 or output_dims[0] < output_dims[1]
        self.output_shape = list(model_output.shape)
        self.model_path = str(model_path)
        self.provider = self.session.get_providers()[0]
        self.confidence_threshold = float(confidence_threshold)
        self.iou_threshold = float(iou_threshold)

    def detect(self, frame: np.ndarray, confidence_threshold: float | None = None) -> tuple[list[tuple[int, int, int, int]], list[float]]:
        original_height, original_width = frame.shape[:2]
        scale = min(self.model_width / original_width, self.model_height / original_height)
        resized_width, resized_height = max(1, int(original_width * scale)), max(1, int(original_height * scale))
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        pad_x, pad_y = (self.model_width - resized_width) / 2, (self.model_height - resized_height) / 2
        padded = cv2.copyMakeBorder(
            resized,
            int(round(pad_y - 0.1)),
            int(round(pad_y + 0.1)),
            int(round(pad_x - 0.1)),
            int(round(pad_x + 0.1)),
            cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        blob = np.ascontiguousarray(np.transpose(cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0, (2, 0, 1))[np.newaxis])
        raw = self.session.run(None, {self.input_name: blob})[0][0]
        predictions = raw.T if self.transpose_output else raw
        threshold = float(confidence_threshold if confidence_threshold is not None else self.confidence_threshold)
        if predictions.shape[1] == 6 and self.output_shape[-1] == 6:
            boxes, confidence, classes = predictions[:, :4].astype(np.float32), predictions[:, 4].astype(np.float32), predictions[:, 5].astype(np.int32)
            mask = (confidence >= threshold) & (classes == 0)
        else:
            centers, scores = predictions[:, :4].astype(np.float32), predictions[:, 4:].astype(np.float32)
            confidence = scores[:, 0]
            mask = confidence >= threshold
            boxes = np.stack(
                [centers[:, 0] - centers[:, 2] / 2, centers[:, 1] - centers[:, 3] / 2,
                 centers[:, 0] + centers[:, 2] / 2, centers[:, 1] + centers[:, 3] / 2],
                axis=1,
            ).astype(np.float32)
        if not mask.any():
            return [], []
        boxes, confidence = boxes[mask], confidence[mask]
        indices = cv2.dnn.NMSBoxes(
            [[float(x1), float(y1), max(0.0, float(x2 - x1)), max(0.0, float(y2 - y1))] for x1, y1, x2, y2 in boxes],
            confidence.tolist(),
            threshold,
            self.iou_threshold,
        )
        results, result_scores = [], []
        for raw_index in np.asarray(indices).reshape(-1):
            index = int(raw_index)
            x1, y1, x2, y2 = boxes[index]
            output = (
                int(np.clip((x1 - pad_x) / scale, 0, original_width)),
                int(np.clip((y1 - pad_y) / scale, 0, original_height)),
                int(np.clip((x2 - pad_x) / scale, 0, original_width)),
                int(np.clip((y2 - pad_y) / scale, 0, original_height)),
            )
            if output[2] > output[0] and output[3] > output[1]:
                results.append(output)
                result_scores.append(float(confidence[index]))
        return results, result_scores


class FaceMosaicProcessor:
    def __init__(
        self,
        *,
        model_path: Path,
        confidence_threshold: float,
        iou_threshold: float,
        mosaic_size: int,
        frame_skip: int,
        intra_op_threads: int,
        ffmpeg_path: Path,
    ) -> None:
        self.detector = FaceDetector(
            model_path,
            confidence_threshold=confidence_threshold,
            iou_threshold=iou_threshold,
            intra_op_threads=intra_op_threads,
        )
        self.confidence_threshold = float(confidence_threshold)
        self.mosaic_size = max(2, int(mosaic_size))
        self.frame_skip = max(0, int(frame_skip))
        self.ffmpeg_path = Path(ffmpeg_path)

    def process_video_file(self, source: Path, target: Path) -> dict[str, object]:
        source, target = Path(source), Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        video_only = target.with_name(f"{target.stem}.video_only{target.suffix}")
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise RuntimeError(f"could not open video: {source}")
        fps = max(float(capture.get(cv2.CAP_PROP_FPS) or 25.0), 1)
        width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        writer = cv2.VideoWriter(str(video_only), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            capture.release()
            raise RuntimeError("could not create temporary video")
        last_boxes: list[tuple[int, int, int, int]] = []
        frames_processed = frames_with_faces = detected_boxes = max_faces_per_frame = detection_runs = 0
        detection_elapsed = 0.0
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if self.frame_skip == 0 or frames_processed % (self.frame_skip + 1) == 0:
                    started = time.perf_counter()
                    last_boxes, _ = self.detector.detect(frame, self.confidence_threshold)
                    detection_elapsed += time.perf_counter() - started
                    detection_runs += 1
                    detected_boxes += len(last_boxes)
                if last_boxes:
                    frames_with_faces += 1
                    max_faces_per_frame = max(max_faces_per_frame, len(last_boxes))
                    mosaic_faces(frame, last_boxes, self.mosaic_size)
                writer.write(frame)
                frames_processed += 1
        finally:
            capture.release()
            writer.release()
        if frames_processed <= 0:
            raise RuntimeError("video has no readable frames")
        analysis: dict[str, object] = {
            "applied": frames_with_faces > 0,
            "backend": "celery-onnx-gpu",
            "model": self.detector.model_path,
            "execution_provider": self.detector.provider,
            "frames_processed": frames_processed,
            "frames_with_faces": frames_with_faces,
            "detected_boxes": detected_boxes,
            "max_faces_per_frame": max_faces_per_frame,
            "detection_runs": detection_runs,
            "average_detection_ms": round(detection_elapsed * 1000 / detection_runs if detection_runs else 0, 2),
            "duration_sec": frames_processed / fps,
            "mosaic_size": self.mosaic_size,
            "frame_skip": self.frame_skip,
        }
        if frames_with_faces <= 0:
            video_only.unlink(missing_ok=True)
            return analysis
        common = [
            str(self.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_only), "-i", str(source), "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-shortest", "-movflags", "+faststart",
        ]
        audio_preserved = False
        for audio_args in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "192k"]):
            process = subprocess.run([*common, *audio_args, str(target)], capture_output=True)
            if process.returncode == 0:
                audio_preserved = True
                break
            target.unlink(missing_ok=True)
        if not target.exists():
            shutil.move(str(video_only), str(target))
        else:
            video_only.unlink(missing_ok=True)
        analysis["audio_preserved"] = audio_preserved
        analysis["output_path"] = str(target)
        return analysis
