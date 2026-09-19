from __future__ import annotations

import argparse
import json
import math
import sys
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as F


SAMPLE_RATE = 48_000
STEMS = ("speech", "music", "sfx")


def _load_model(source_dir: Path, checkpoint: Path, device: torch.device) -> torch.nn.Module:
    if not (source_dir / "src" / "models" / "bandit" / "bandit.py").is_file():
        raise RuntimeError("Bandit v2 inference source is missing")
    if not checkpoint.is_file():
        raise RuntimeError("Bandit v2 checkpoint is missing")
    lightning_stub = types.ModuleType("pytorch_lightning")
    lightning_stub.LightningModule = torch.nn.Module
    sys.modules.setdefault("pytorch_lightning", lightning_stub)
    source_value = str(source_dir)
    if source_value not in sys.path:
        sys.path.insert(0, source_value)

    from src.models.bandit.bandit import Bandit  # pylint: disable=import-error,import-outside-toplevel

    model = Bandit(
        in_channels=1,
        stems=list(STEMS),
        fs=SAMPLE_RATE,
        band_type="musical",
        n_bands=64,
        normalize_channel_independently=False,
        treat_channel_as_feature=True,
        n_sqm_modules=8,
        emb_dim=128,
        rnn_dim=256,
        bidirectional=True,
        rnn_type="GRU",
        mlp_dim=512,
        hidden_activation="Tanh",
        hidden_activation_kwargs=None,
        complex_mask=True,
        use_freq_weights=True,
        n_fft=2048,
        win_length=2048,
        hop_length=512,
        window_fn="hann_window",
        wkwargs=None,
        power=None,
        center=True,
        normalized=True,
        pad_mode="reflect",
        onesided=True,
    )
    payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
    state = payload.get("state_dict", payload)
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise RuntimeError(f"Bandit checkpoint mismatch: missing={missing}, unexpected={unexpected}")
    return model.eval().to(device)


def _reflect_pad(audio: torch.Tensor, left: int, right: int) -> torch.Tensor:
    while left or right:
        maximum = max(audio.shape[-1] - 1, 0)
        if maximum == 0:
            return F.pad(audio, (left, right), mode="constant")
        step_left = min(left, maximum)
        step_right = min(right, maximum)
        audio = F.pad(audio, (step_left, step_right), mode="reflect")
        left -= step_left
        right -= step_right
    return audio


def _separate(
    model: torch.nn.Module,
    audio: torch.Tensor,
    device: torch.device,
    batch_size: int,
) -> dict[str, torch.Tensor]:
    chunk_samples = 8 * SAMPLE_RATE
    hop_samples = SAMPLE_RATE
    overlap_samples = chunk_samples - hop_samples
    front_pad = 2 * overlap_samples
    sample_count = audio.shape[-1]
    chunk_count = math.ceil(
        (sample_count + 2 * front_pad - chunk_samples) / hop_samples
    ) + 1
    padded_samples = (chunk_count - 1) * hop_samples + chunk_samples
    padded = _reflect_pad(audio, front_pad, padded_samples - sample_count - front_pad)
    chunks = padded.unfold(-1, chunk_samples, hop_samples).squeeze(0)
    channels = chunks.shape[0]
    window = torch.hann_window(chunk_samples, dtype=torch.float32) / 4.0
    estimates = {
        stem: torch.zeros((channels, padded_samples), dtype=torch.float32)
        for stem in STEMS
    }

    with torch.inference_mode():
        for start in range(0, chunk_count, batch_size):
            end = min(start + batch_size, chunk_count)
            batch = chunks[:, start:end, :].reshape(-1, 1, chunk_samples).to(device)
            output = model({"mixture": {"audio": batch}})["estimates"]
            for stem in STEMS:
                stem_batch = (
                    output[stem]["audio"]
                    .reshape(channels, end - start, chunk_samples)
                    .float()
                    .cpu()
                )
                for offset in range(end - start):
                    write_start = (start + offset) * hop_samples
                    estimates[stem][:, write_start : write_start + chunk_samples] += (
                        stem_batch[:, offset, :] * window
                    )
            del batch, output

    return {
        stem: value[:, front_pad : front_pad + sample_count].contiguous()
        for stem, value in estimates.items()
    }


def _audio_stats(audio: np.ndarray) -> dict[str, float]:
    return {
        "rms_dbfs": float(20 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-12)),
        "peak_dbfs": float(20 * np.log10(np.max(np.abs(audio)) + 1e-12)),
        "clipped_fraction": float(np.mean(np.abs(audio) >= 1.0)),
    }


def _limit_sample_peak(audio: np.ndarray, limit_dbfs: float) -> tuple[np.ndarray, float]:
    peak = float(np.max(np.abs(audio)))
    target = 10 ** (limit_dbfs / 20)
    if peak <= target:
        return audio, 0.0
    gain = target / peak
    return audio * gain, float(20 * math.log10(gain))


def separate_audio(
    source: Path,
    output_dir: Path,
    source_dir: Path,
    checkpoint: Path,
    *,
    batch_size: int = 4,
    peak_limit_dbfs: float = -1.0,
) -> dict[str, Any]:
    raw_audio, sample_rate = sf.read(source, dtype="float32", always_2d=True)
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"expected {SAMPLE_RATE} Hz, got {sample_rate}")
    if raw_audio.shape[1] != 2:
        raise ValueError(f"expected stereo audio, got {raw_audio.shape[1]} channels")
    audio = torch.from_numpy(raw_audio.T.copy()).unsqueeze(0)
    device = torch.device("cuda")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.cuda.reset_peak_memory_stats(device)
    torch.cuda.synchronize(device)
    model: torch.nn.Module | None = None
    try:
        load_started = time.perf_counter()
        model = _load_model(source_dir, checkpoint, device)
        torch.cuda.synchronize(device)
        load_seconds = time.perf_counter() - load_started
        inference_started = time.perf_counter()
        estimates = _separate(model, audio, device, batch_size)
        torch.cuda.synchronize(device)
        inference_seconds = time.perf_counter() - inference_started

        output_dir.mkdir(parents=True, exist_ok=True)
        raw_stems = {stem: tensor.numpy().T for stem, tensor in estimates.items()}
        raw_stems["background"] = raw_stems["music"] + raw_stems["sfx"]
        outputs: dict[str, str] = {}
        stem_metrics: dict[str, dict[str, float]] = {}
        for stem, raw_stem in raw_stems.items():
            delivered, gain_db = _limit_sample_peak(raw_stem, peak_limit_dbfs)
            target = output_dir / f"{stem}.wav"
            sf.write(target, delivered, SAMPLE_RATE, subtype="PCM_16")
            outputs[stem] = str(target)
            stem_metrics[stem] = {
                **_audio_stats(raw_stem),
                "delivery_gain_db": gain_db,
            }

        reconstructed = sum(raw_stems[stem] for stem in STEMS)
        error = raw_audio - reconstructed
        return {
            "duration_seconds": round(raw_audio.shape[0] / SAMPLE_RATE, 6),
            "sample_rate": SAMPLE_RATE,
            "channels": raw_audio.shape[1],
            "batch_size": batch_size,
            "load_seconds": round(load_seconds, 4),
            "inference_seconds": round(inference_seconds, 4),
            "rtf": round(inference_seconds / (raw_audio.shape[0] / SAMPLE_RATE), 6),
            "reconstruction_snr_db": round(
                10
                * math.log10(
                    (float(np.mean(raw_audio**2)) + 1e-20)
                    / (float(np.mean(error**2)) + 1e-20)
                ),
                4,
            ),
            "peak_limit_dbfs": peak_limit_dbfs,
            "peak_cuda_allocated_mib": round(
                torch.cuda.max_memory_allocated(device) / 1024**2, 2
            ),
            "peak_cuda_reserved_mib": round(
                torch.cuda.max_memory_reserved(device) / 1024**2, 2
            ),
            "stems": stem_metrics,
            "output_paths": outputs,
        }
    finally:
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--peak-limit-dbfs", type=float, default=-1.0)
    args = parser.parse_args()
    result = separate_audio(
        args.input,
        args.output_dir,
        args.source_dir,
        args.checkpoint,
        batch_size=args.batch_size,
        peak_limit_dbfs=args.peak_limit_dbfs,
    )
    args.result_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
