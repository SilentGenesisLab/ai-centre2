#!/usr/bin/env python3
"""Run the Bandit v2 multilingual DnR model without training dependencies."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import types
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as F


SAMPLE_RATE = 48_000
STEMS = ("speech", "music", "sfx")


def _load_model(repo: Path, checkpoint: Path, device: torch.device) -> torch.nn.Module:
    # Bandit's inference model only needs LightningModule as a base class.  Avoid
    # importing the repository's training stack (Ray and Lightning) on production.
    lightning_stub = types.ModuleType("pytorch_lightning")
    lightning_stub.LightningModule = torch.nn.Module
    sys.modules.setdefault("pytorch_lightning", lightning_stub)
    sys.path.insert(0, str(repo))

    from src.models.bandit.bandit import Bandit  # pylint: disable=import-error

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
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise RuntimeError(f"checkpoint mismatch: missing={missing}, unexpected={unexpected}")
    return model.eval().to(device)


def _reflect_pad(audio: torch.Tensor, left: int, right: int) -> torch.Tensor:
    # torch reflect padding requires each pad to be shorter than the signal.
    while left or right:
        max_pad = max(audio.shape[-1] - 1, 0)
        if max_pad == 0:
            return F.pad(audio, (left, right), mode="constant")
        step_left = min(left, max_pad)
        step_right = min(right, max_pad)
        audio = F.pad(audio, (step_left, step_right), mode="reflect")
        left -= step_left
        right -= step_right
    return audio


def separate(
    model: torch.nn.Module,
    audio: torch.Tensor,
    device: torch.device,
    batch_size: int,
) -> dict[str, torch.Tensor]:
    chunk_samples = 8 * SAMPLE_RATE
    hop_samples = SAMPLE_RATE
    overlap_samples = chunk_samples - hop_samples
    front_pad = 2 * overlap_samples
    n_samples = audio.shape[-1]
    n_chunks = math.ceil(
        (n_samples + 2 * front_pad - chunk_samples) / hop_samples
    ) + 1
    padded_samples = (n_chunks - 1) * hop_samples + chunk_samples
    right_pad = padded_samples - n_samples - front_pad
    padded = _reflect_pad(audio, front_pad, right_pad)
    chunks = padded.unfold(-1, chunk_samples, hop_samples).squeeze(0)
    # chunks: channels, n_chunks, chunk_samples
    channels = chunks.shape[0]
    window = torch.hann_window(chunk_samples, dtype=torch.float32) / 4.0
    estimates = {
        stem: torch.zeros((channels, padded_samples), dtype=torch.float32)
        for stem in STEMS
    }

    with torch.inference_mode():
        for start in range(0, n_chunks, batch_size):
            end = min(start + batch_size, n_chunks)
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
                    estimates[stem][
                        :, write_start : write_start + chunk_samples
                    ] += stem_batch[:, offset, :] * window
            del batch, output

    return {
        stem: value[:, front_pad : front_pad + n_samples].contiguous()
        for stem, value in estimates.items()
    }


def _audio_stats(audio: np.ndarray) -> dict[str, float]:
    return {
        "rms_dbfs": float(20 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-12)),
        "peak_dbfs": float(20 * np.log10(np.max(np.abs(audio)) + 1e-12)),
        "clipped_fraction": float(np.mean(np.abs(audio) >= 1.0)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()

    raw_audio, sample_rate = sf.read(args.input, dtype="float32", always_2d=True)
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"expected {SAMPLE_RATE} Hz, got {sample_rate}")
    audio = torch.from_numpy(raw_audio.T.copy()).unsqueeze(0)
    device = torch.device(args.device)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
        torch.cuda.synchronize(device)

    load_start = time.perf_counter()
    model = _load_model(args.repo, args.checkpoint, device)
    load_seconds = time.perf_counter() - load_start
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    infer_start = time.perf_counter()
    estimates = separate(model, audio, device, args.batch_size)
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    inference_seconds = time.perf_counter() - infer_start

    args.output.mkdir(parents=True, exist_ok=True)
    stem_arrays: dict[str, np.ndarray] = {}
    for stem, tensor in estimates.items():
        array = tensor.numpy().T
        stem_arrays[stem] = array
        sf.write(args.output / f"{stem}.wav", array, SAMPLE_RATE, subtype="FLOAT")
    background = stem_arrays["music"] + stem_arrays["sfx"]
    sf.write(args.output / "background.wav", background, SAMPLE_RATE, subtype="FLOAT")

    reconstructed = sum(stem_arrays.values())
    error = raw_audio - reconstructed
    mix_power = float(np.mean(raw_audio**2))
    error_power = float(np.mean(error**2))
    result = {
        "input": str(args.input),
        "duration_seconds": round(raw_audio.shape[0] / SAMPLE_RATE, 6),
        "sample_rate": SAMPLE_RATE,
        "channels": raw_audio.shape[1],
        "batch_size": args.batch_size,
        "load_seconds": round(load_seconds, 4),
        "inference_seconds": round(inference_seconds, 4),
        "rtf": round(inference_seconds / (raw_audio.shape[0] / SAMPLE_RATE), 6),
        "reconstruction_snr_db": round(
            10 * math.log10((mix_power + 1e-20) / (error_power + 1e-20)), 4
        ),
        "mix": _audio_stats(raw_audio),
        "speech": _audio_stats(stem_arrays["speech"]),
        "music": _audio_stats(stem_arrays["music"]),
        "sfx": _audio_stats(stem_arrays["sfx"]),
        "background": _audio_stats(background),
        "peak_cuda_allocated_mib": round(
            torch.cuda.max_memory_allocated(device) / 1024**2, 2
        )
        if device.type == "cuda"
        else 0,
        "peak_cuda_reserved_mib": round(
            torch.cuda.max_memory_reserved(device) / 1024**2, 2
        )
        if device.type == "cuda"
        else 0,
    }
    (args.output / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
