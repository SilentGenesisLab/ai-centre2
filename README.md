# AI Centre 2

Production control plane and GPU services for the AI video translation pipeline.

## Services

- `face_worker`: existing asynchronous face mosaic service.
- `OCR`: PP-OCRv6 and Thai PP-OCRv5 gateway/workers.
- `control_plane`: ASR/TTS job gateway and per-GPU lifecycle control.
- `managed_backends`: optional GPU1-only model servers.
- `deploy`: user-systemd units and managed backend templates.

Runtime environments, model weights, generated media and secrets are deliberately
excluded from Git.

## Deployment target

```text
/home/donxu/ai-centre
```

GPU services run as separate OS processes. A GPU can be drained and disabled by
stopping only the allowlisted user services assigned to it.

## Control-plane endpoints

- `GET /health`
- `POST /v1/asr/transcriptions`
- `POST /v1/tts/speech`
- `GET /v1/admin/gpus`
- `POST /v1/admin/gpus/{gpu_id}/drain`
- `POST /v1/admin/gpus/{gpu_id}/disable`
- `POST /v1/admin/gpus/{gpu_id}/enable`

See `docs/AUDIO_GPU_OPERATIONS.md` for deployment and GPU reservation details.

Complete request/response examples are documented in `docs/API.md`.
