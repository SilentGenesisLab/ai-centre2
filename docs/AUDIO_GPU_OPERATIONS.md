# Audio and GPU operations

## Runtime layout

The production control plane is CPU-only and listens on `127.0.0.1:8320`.
It proxies:

- ASR to `http://127.0.0.1:9001`.
- VoxCPM2 to `http://127.0.0.1:8191`.

The backend URLs are configured through `.env`. This allows the control plane to
reuse existing model servers or switch to AI Centre-managed model servers without
changing client integrations.

## GPU memory budget

When both managed backends are placed on physical GPU 1:

| Component | Expected VRAM |
| --- | ---: |
| VoxCPM2 with a 0.40 vLLM budget | 12.8 GiB reserved |
| faster-whisper large-v3, batch 8 | 5–7 GiB |
| CUDA and voice-cache overhead | 1–2 GiB |
| Total | approximately 19–22 GiB |

Actual peak memory must be recorded with `nvidia-smi` during the server benchmark.
Do not leave vLLM at its default 0.9 memory utilization when sharing a GPU.

## Physical and logical GPU numbers

Every managed backend is started with:

```text
CUDA_VISIBLE_DEVICES=1
```

The process therefore sees physical GPU 1 as logical `cuda:0`. The backend must
use `device_index=0`; using `device_index=1` would be incorrect.

## Reserving GPU 0

GPU lifecycle operations only touch this allowlist:

```text
ai-centre-face-worker-gpu0.service
ai-centre-ocr-worker@0.service
```

No PID is killed directly and no service owned by another Linux user is managed.

Disable GPU 0:

```bash
source /home/donxu/ai-centre/.env
/home/donxu/ai-centre/scripts/reserve_gpu0.sh
```

Re-enable GPU 0:

```bash
curl -X POST \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  http://127.0.0.1:8320/v1/admin/gpus/0/enable
```

`drain` stops the services for the current session. `disable` stops them and
removes their autostart links. Celery receives `SIGTERM`, so an active job can
finish within the unit's stop timeout.

## Managed backend migration

The `.service.example` units are intentionally not installed by
`install_control_plane.sh`. Before enabling them:

1. Stop the existing service that owns the same port.
2. Create the isolated `.venv-asr` or `.venv-tts` environment.
3. Benchmark one backend at a time.
4. Confirm GPU 0 remains free.
5. Copy the selected example unit without `.example`.
6. Enable the unit with `systemctl --user enable --now`.

Do not run two VoxCPM2 copies on GPU 1: the server currently has insufficient
free memory for a safe duplicate.

