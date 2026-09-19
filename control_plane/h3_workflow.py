from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import imageio_ffmpeg


REQUIRED_NODES = (
    "LoadVideo",
    "LoadImage",
    "LoadAudio",
    "GetVideoComponents",
    "VAELoader",
    "VAEDecodeAudio",
    "VAEDecode",
    "KSamplerSelect",
    "BasicScheduler",
    "SamplerCustomAdvanced",
    "BasicGuider",
    "UNETLoader",
    "CLIPLoader",
    "RandomNoise",
    "CreateVideo",
    "MiniMaxH3ReferenceToVideo",
    "LoraLoaderModelOnly",
    "MiniMaxH3SigmaShift",
    "SaveVideo",
)

REQUIRED_MODELS = (
    "minimax_h3_video_vae_fp16.safetensors",
    "minimax_h3_audio_vae_fp32.safetensors",
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
)


class H3WorkerError(RuntimeError):
    """A remote worker failure that may be retried on another worker."""


class H3WorkerIncompatible(H3WorkerError):
    """The remote worker cannot execute the fixed H3 graph."""


class H3Cancelled(RuntimeError):
    pass


def probe_video(path: Path) -> tuple[float, bool]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
    if not match:
        raise ValueError("reference video cannot be decoded")
    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    has_audio = bool(re.search(r"Stream #.*Audio:", completed.stderr))
    if duration <= 0:
        raise ValueError("reference video duration must be positive")
    return duration, has_audio


def validate_part_durations(total: float, mode: str, split_seconds: float | None) -> list[float]:
    if mode == "single":
        if not 2 <= total <= 15:
            raise ValueError("single reference video duration must be between 2 and 15 seconds")
        return [total]
    if not 4 <= total <= 30:
        raise ValueError("two-part reference video duration must be between 4 and 30 seconds")
    split = total / 2 if split_seconds is None else float(split_seconds)
    parts = [split, total - split]
    if any(value < 2 or value > 15 for value in parts):
        raise ValueError("each reference segment must be between 2 and 15 seconds")
    return parts


def split_reference(source: Path, work_dir: Path, durations: list[float]) -> list[Path]:
    if len(durations) == 1:
        return [source]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    parts: list[Path] = []
    start = 0.0
    for index, duration in enumerate(durations, start=1):
        target = work_dir / f"reference-part-{index}.mp4"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{duration:.6f}",
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not target.is_file() or not target.stat().st_size:
            raise RuntimeError(f"reference video split failed: {completed.stderr.strip()[-500:]}")
        parts.append(target)
        start += duration
    return parts


def frame_length(seconds: float) -> int:
    frames = max(5, round(seconds * 24))
    return frames + (5 - frames % 17) % 17


DELIVERY_DIMENSIONS: dict[str, dict[str, tuple[int, int]]] = {
    "480p": {
        "9:16": (480, 854), "16:9": (854, 480), "1:1": (480, 480),
        "4:3": (640, 480), "3:4": (480, 640),
    },
    "720p": {
        # H3's native 768-class canvas is the production "720p" tier.
        # Keep generation and delivery on the native grid; do not generate at
        # 720x1280 and do not hide a resize behind this preset.
        "9:16": (768, 1344), "16:9": (1344, 768), "1:1": (768, 768),
        "4:3": (1024, 768), "3:4": (768, 1024),
    },
    "1080p": {
        "9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080),
        "4:3": (1440, 1080), "3:4": (1080, 1440),
    },
}

H3_QUALITY_PROFILES: dict[str, dict[str, object]] = {
    "low": {"max_model_pixels": 414_720, "steps": 4, "sampler": "euler", "scheduler": "simple", "turbo_lora": True},
    "medium": {"max_model_pixels": 1_048_576, "steps": 4, "sampler": "euler", "scheduler": "simple", "turbo_lora": True},
    "high": {"max_model_pixels": 1_048_576, "steps": 4, "sampler": "res_multistep", "scheduler": "simple", "turbo_lora": True},
}


def quality_profile(quality: str) -> dict[str, object]:
    normalized = "medium" if quality == "midia" else quality
    try:
        return H3_QUALITY_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError("quality must be low, medium, midia, or high") from exc


def resolve_dimensions(
    resolution: str,
    aspect_ratio: str,
    custom_width: int | None = None,
    custom_height: int | None = None,
    quality: str = "medium",
) -> tuple[int, int, int, int]:
    """Return model width/height and final delivery width/height."""
    if custom_width is not None or custom_height is not None:
        if custom_width is None or custom_height is None:
            raise ValueError("custom width and height must be provided together")
        if custom_width % 32 or custom_height % 32:
            raise ValueError("custom width and height must be multiples of 32")
        if custom_width > 1344 or custom_height > 1344 or custom_width * custom_height > 1_048_576:
            raise ValueError("custom generation dimensions exceed the safe model limit")
        return custom_width, custom_height, custom_width, custom_height
    try:
        output_width, output_height = DELIVERY_DIMENSIONS[resolution][aspect_ratio]
    except KeyError as exc:
        raise ValueError("unsupported resolution or aspect ratio") from exc
    max_model_pixels = int(quality_profile(quality)["max_model_pixels"])
    scale = min(1.0, (max_model_pixels / (output_width * output_height)) ** 0.5)
    model_width = max(32, int(output_width * scale) // 32 * 32)
    model_height = max(32, int(output_height * scale) // 32 * 32)
    return model_width, model_height, output_width, output_height


def build_prompt_graph(
    video_name: str | None,
    prompt: str,
    seconds: float,
    width: int,
    height: int,
    seed: int,
    filename_prefix: str,
    *,
    reference_image_name: str | None = None,
    reference_audio_name: str | None = None,
    reference_has_audio: bool = True,
    video_names: list[str] | None = None,
    reference_image_names: list[str] | None = None,
    reference_audio_names: list[str] | None = None,
    reference_video_audio_flags: list[bool] | None = None,
    quality: str = "medium",
) -> dict[str, Any]:
    """Build only the approved MiniMax H3 graph; prompt is passed byte-for-byte."""
    profile = quality_profile(quality)
    model_source = ["141", 0] if bool(profile["turbo_lora"]) else ["127", 0]
    graph: dict[str, Any] = {
        "119": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "120": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "121": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}},
        "122": {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}},
        "123": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": str(profile["sampler"])}},
        "124": {"class_type": "BasicScheduler", "inputs": {"model": ["142", 0], "scheduler": str(profile["scheduler"]), "steps": int(profile["steps"]), "denoise": 1.0}},
        "125": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": ["124", 0], "latent_image": ["136", 1]}},
        "126": {"class_type": "BasicGuider", "inputs": {"model": ["142", 0], "conditioning": ["136", 0]}},
        "127": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "130": {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "fps": 24.0, "audio": ["121", 0], "bit_depth": 8}},
        "141": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["127", 0], "lora_name": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "strength_model": 1.0}},
        "142": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": model_source, "shift_video": 12.0, "shift_audio": 3.0}},
        "92": {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "filename_prefix": filename_prefix, "format": "mp4", "codec": "auto"}},
    }
    h3_inputs: dict[str, Any] = {
        "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
        "prompt": prompt, "width": width, "height": height,
        "length": frame_length(seconds), "ref_image_size": "max",
    }
    videos = list(video_names or ([] if video_name is None else [video_name]))
    images = list(reference_image_names or ([] if reference_image_name is None else [reference_image_name]))
    audios = list(reference_audio_names or ([] if reference_audio_name is None else [reference_audio_name]))
    audio_flags = list(reference_video_audio_flags or [reference_has_audio] * len(videos))
    for index, name in enumerate(videos):
        load_id, components_id = (("1", "2") if index == 0 else (str(200 + index * 2), str(201 + index * 2)))
        graph[load_id] = {"class_type": "LoadVideo", "inputs": {"file": name}}
        graph[components_id] = {"class_type": "GetVideoComponents", "inputs": {"video": [load_id, 0]}}
        h3_inputs[f"ref_videos.ref_video_{index}"] = [components_id, 0]
        if index < len(audio_flags) and audio_flags[index]:
            h3_inputs[f"ref_video_audios.ref_video_audio_{index}"] = [components_id, 1]
    for index, name in enumerate(images):
        node_id = "3" if index == 0 else str(220 + index)
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        h3_inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]
    for index, name in enumerate(audios):
        node_id = "4" if index == 0 else str(240 + index)
        graph[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": name}}
        h3_inputs[f"ref_audios.ref_audio_{index}"] = [node_id, 0]
    graph["136"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": h3_inputs}
    return graph


class ComfyH3Client:
    def __init__(self, base_url: str, timeout_seconds: float = 120) -> None:
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=10),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def upload(self, path: Path, remote_name: str) -> str:
        try:
            with path.open("rb") as stream:
                response = self.client.post(
                    "/upload/image",
                    files={"image": (remote_name, stream, "application/octet-stream")},
                    data={"type": "input", "overwrite": "true"},
                )
            response.raise_for_status()
            return str(response.json()["name"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise H3WorkerError("worker media upload failed") from exc

    def submit(self, graph: dict[str, Any], client_id: str) -> str:
        try:
            response = self.client.post("/prompt", json={"prompt": graph, "client_id": client_id})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise H3WorkerError("worker prompt submission failed") from exc
        if payload.get("node_errors"):
            raise H3WorkerIncompatible("worker rejected the fixed H3 workflow")
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise H3WorkerError("worker did not return a prompt id")
        return prompt_id

    def wait_and_download(
        self,
        prompt_id: str,
        output: Path,
        poll_seconds: float,
        timeout_seconds: float,
        on_poll: Callable[[], None],
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        consecutive_poll_errors = 0
        while time.monotonic() < deadline:
            on_poll()
            try:
                response = self.client.get(f"/history/{prompt_id}")
                response.raise_for_status()
                history = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                # Public ComfyUI tunnels can briefly time out while the GPU is busy.
                # A single failed history poll must not turn an active generation into
                # a failover/re-submission, otherwise the same expensive job may run twice.
                consecutive_poll_errors += 1
                if consecutive_poll_errors >= 12:
                    raise H3WorkerError("worker became unavailable during generation") from exc
                time.sleep(poll_seconds)
                continue
            consecutive_poll_errors = 0
            if prompt_id not in history:
                time.sleep(poll_seconds)
                continue
            record = history[prompt_id]
            if record.get("status", {}).get("status_str") != "success":
                raise H3WorkerError("worker generation failed")
            output_item = self._output_item(record)
            try:
                with self.client.stream("GET", "/view", params=output_item) as response:
                    response.raise_for_status()
                    with output.open("wb") as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            stream.write(chunk)
            except httpx.HTTPError as exc:
                output.unlink(missing_ok=True)
                raise H3WorkerError("worker result download failed") from exc
            if not output.is_file() or not output.stat().st_size:
                raise H3WorkerError("worker returned an empty result")
            return
        raise H3WorkerError("worker generation timed out")

    @staticmethod
    def _output_item(record: dict[str, Any]) -> dict[str, Any]:
        outputs = record.get("outputs", {})
        preferred = outputs.get("92", {}) if isinstance(outputs, dict) else {}
        for node in [preferred, *(outputs.values() if isinstance(outputs, dict) else [])]:
            if not isinstance(node, dict):
                continue
            for key in ("images", "videos", "gifs"):
                items = node.get(key)
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    return items[0]
        raise H3WorkerError("worker result does not contain a video")

    def free_temporary(self) -> None:
        try:
            self.client.post("/free", json={"free_memory": True, "unload_models": False})
        except httpx.HTTPError:
            pass

    def cancel(self, prompt_id: str) -> None:
        try:
            self.client.post("/queue", json={"delete": [prompt_id]}).raise_for_status()
        except httpx.HTTPError:
            try:
                self.client.post("/interrupt").raise_for_status()
            except httpx.HTTPError:
                pass


def concatenate_parts(parts: list[Path], target: Path) -> None:
    if len(parts) == 1:
        import shutil

        shutil.copyfile(parts[0], target)
        return
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    all_have_audio = all(probe_video(path)[1] for path in parts)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for part in parts:
        command.extend(["-i", str(part)])
    if all_have_audio:
        command.extend([
            "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-c:a", "aac", "-b:a", "192k",
        ])
    else:
        command.extend([
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        ])
    command.extend(["-movflags", "+faststart", str(target)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not target.is_file() or not target.stat().st_size:
        raise RuntimeError(f"generated video concatenation failed: {completed.stderr.strip()[-500:]}")


def render_delivery(
    source: Path,
    target: Path,
    width: int,
    height: int,
    duration_seconds: float | None = None,
) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-c:a", "aac", "-b:a", "192k",
    ]
    if duration_seconds is not None:
        command.extend(["-t", f"{duration_seconds:.6f}"])
    command.extend(["-movflags", "+faststart", str(target)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not target.is_file() or not target.stat().st_size:
        raise RuntimeError(f"delivery video rendering failed: {completed.stderr.strip()[-500:]}")
