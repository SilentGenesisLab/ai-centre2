# AI Centre 2

Production control plane and GPU services for the AI video translation pipeline.

## Services

- `face_worker`: existing asynchronous face mosaic service.
- `OCR`: PP-OCRv6 and Thai PP-OCRv5 gateway/workers.
- `control_plane`: ASR/TTS job gateway and per-GPU lifecycle control.
- `managed_backends`: optional GPU1-only model servers.
- `gateway`: Caddy HTTPS reverse proxy for one-public-IP deployments.
- `deploy`: user-systemd units and managed backend templates.

Runtime environments, model weights, generated media and secrets are deliberately
excluded from Git.

## Deployment target

```text
/home/donxu/ai-centre
```

GPU services run as separate OS processes. A GPU can be drained and disabled by
stopping only the allowlisted user services assigned to it.

## Public access

- Base URL: `http://aicentre2.sligenai.cn:8320`
- Swagger: `http://aicentre2.sligenai.cn:8320/docs`
- OpenAPI: `http://aicentre2.sligenai.cn:8320/openapi.json`
- Health: `http://aicentre2.sligenai.cn:8320/health`

Port `8320` is currently exposed through router NAT. HTTP is available for
integration, but HTTPS is still pending because public ports 80 and 443 terminate
on the router instead of this server.

## Control-plane endpoints

- `GET /health`
- `POST /v1/asr/transcriptions`
- `POST /v1/tts/speech`
- `GET /v1/admin/gpus`
- `POST /v1/admin/gpus/{gpu_id}/drain`
- `POST /v1/admin/gpus/{gpu_id}/disable`
- `POST /v1/admin/gpus/{gpu_id}/enable`

See `docs/AUDIO_GPU_OPERATIONS.md` for deployment and GPU reservation details.
See `docs/HTTPS_GATEWAY.md` for the shared-public-IP Caddy deployment.

Complete request/response examples are documented in `docs/API.md`.
Public IP, NAT and internet-facing usage are documented in `docs/PUBLIC_API.md`.
