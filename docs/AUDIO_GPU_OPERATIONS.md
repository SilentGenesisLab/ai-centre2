# Audio and GPU operations

## Runtime layout

The production control plane is CPU-only and listens on `127.0.0.1:8320`.
It proxies:

- ASR to the managed Faster-Whisper service on `http://127.0.0.1:9001`.
- TTS to the compatibility gateway on `http://127.0.0.1:8193`.

The compatibility gateway forwards to the managed vLLM-Omni VoxCPM2 server on
`http://127.0.0.1:8192`. The adapter preserves the existing `/tts` and
`/clone_path` contract while the model server uses the official
OpenAI-compatible `/v1/audio/speech` endpoint.

The backend URLs are configured through `.env`. The production values are:

```dotenv
ASR_BACKEND_URL=http://127.0.0.1:9001
TTS_BACKEND_URL=http://127.0.0.1:8193
```

## Why user-systemd

The server already runs the control plane, OCR, face worker and gateway as
`donxu` user services. User-systemd keeps the managed audio services consistent
with that operating model and provides:

- startup at boot through user lingering;
- isolated Python environments and model directories;
- bounded stop time and automatic restart on process failure;
- journald logs and systemd status reporting;
- direct NVIDIA access without adding Docker and NVIDIA Container Toolkit.

## GPU allocation

The managed services are split across the two physical GPUs to coexist with
the live OCR, face and other-user workloads:

| Component | Physical GPU | Budget or observed VRAM |
| --- | ---: | ---: |
| Faster-Whisper large-v3, batch 8 | 0 | approximately 4-5 GiB |
| VoxCPM2, maximum four sequences | 1 | 0.35 vLLM budget, approximately 11 GiB |

Each service sets `CUDA_VISIBLE_DEVICES` to one physical GPU, so the selected
card is exposed to the process as logical `cuda:0`. Faster-Whisper must
therefore retain `device_index=0`, even though the two unit files select
different physical GPUs.

Both GPUs must be reserved through operational policy. Consumer RTX GPUs do
not provide MIG isolation, so systemd cannot prevent another Linux user from
allocating the same GPU. Check `nvidia-smi` before every restart or deployment;
reduce the vLLM memory budget or move a workload if the free-memory margin is
below 4 GiB.

## Installation

```bash
cd /home/donxu/ai-centre
bash scripts/install_managed_audio.sh
```

The installer:

1. Creates `.venv-asr` with Python 3.11.
2. Installs CUDA 12 cuBLAS/cuDNN and Faster-Whisper.
3. Downloads `Systran/faster-whisper-large-v3` into `models/`.
4. Creates `.venv-tts` with Python 3.12.
5. Installs vLLM and the pinned vLLM-Omni source revision.
6. Downloads `openbmb/VoxCPM2` into `models/`.
7. Installs and starts the three user-systemd services.

## Verification

Verify the new backends before changing `.env`:

```bash
bash scripts/verify_managed_audio.sh
```

Inspect operations:

```bash
systemctl --user status ai-centre-asr-gpu0.service
systemctl --user status ai-centre-voxcpm2-gpu1.service
systemctl --user status ai-centre-tts-backend.service

journalctl --user -u ai-centre-asr-gpu0.service -n 100 --no-pager
journalctl --user -u ai-centre-voxcpm2-gpu1.service -n 100 --no-pager
journalctl --user -u ai-centre-tts-backend.service -n 100 --no-pager
```

## Control-plane cutover

After the real ASR and TTS verification succeeds:

```bash
sed -i \
  's#^ASR_BACKEND_URL=.*#ASR_BACKEND_URL=http://127.0.0.1:9001#' \
  /home/donxu/ai-centre/.env
sed -i \
  's#^TTS_BACKEND_URL=.*#TTS_BACKEND_URL=http://127.0.0.1:8193#' \
  /home/donxu/ai-centre/.env

systemctl --user restart ai-centre-control.service
systemctl --user restart ai-centre-tts-worker.service
```

Rollback only requires restoring the previous backend URL in `.env` and
restarting the two control services.
