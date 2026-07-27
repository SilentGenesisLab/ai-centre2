# AI Centre 2

Production control plane and GPU services for the AI video translation pipeline.

## Services

- `face_worker`: existing asynchronous face mosaic service.
- `OCR`: PP-OCRv6 and Thai PP-OCRv5 gateway/workers.
- `control_plane`: ASR/TTS job gateway and per-GPU lifecycle control.
- `deploy`: user-systemd units and managed backend templates.

Runtime environments, model weights, generated media and secrets are deliberately
excluded from Git.

## Deployment target

```text
/home/donxu/ai-centre
```

GPU services run as separate OS processes. A GPU can be drained and disabled by
stopping only the allowlisted user services assigned to it.

