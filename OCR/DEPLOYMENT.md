# Enterprise OCR deployment

This directory is the production OCR boundary used by the video translation
pipeline. Clients call one batch API. The gateway distributes requests across
two persistent GPU workers.

```text
video pipeline
    |
    v
OCR gateway :8096
    |-------------------|
    v                   v
GPU 0 worker :8097      GPU 1 worker :8098
PP-OCRv6 detection      PP-OCRv6 detection
PP-OCRv6 Latin rec      PP-OCRv6 Latin rec
PP-OCRv5 Thai rec       PP-OCRv5 Thai rec
```

## Runtime contract

- Detection: `PP-OCRv6_medium_det`
- Chinese/English/Spanish/Portuguese recognition: `PP-OCRv6_medium_rec`
- Thai recognition: `th_PP-OCRv5_mobile_rec`
- PaddlePaddle GPU: `3.3.0`
- PaddleOCR: `3.7.0`
- PaddleX: `3.7.2`
- Python: `3.11`
- One persistent worker per GPU; the gateway performs least-inflight
  round-robin selection and retries another worker on transport or 5xx errors.
- Models are preloaded. `/health` is healthy only when both workers are ready.

## API

Health:

```bash
curl http://127.0.0.1:8096/health
```

Prometheus metrics:

```bash
curl http://127.0.0.1:8096/metrics
```

Batch OCR using a server-local image:

```bash
curl -X POST http://127.0.0.1:8096/v1/ocr/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "video-frame-001",
    "source_lang_hint": "es",
    "images": [{
      "image_id": "frame-0001",
      "path": "/srv/video-frames/frame-0001.jpg",
      "time": 0.0,
      "regions": [
        {"name": "top", "bbox": [0, 0, 1080, 960]},
        {"name": "bottom", "bbox": [0, 960, 1080, 1920]}
      ]
    }]
  }'
```

When the caller cannot share a filesystem with the OCR account, send
`image_base64` instead of `path`. Exactly one image source is required.

## Operations

```bash
systemctl --user status ai-centre-ocr-gateway
systemctl --user status 'ai-centre-ocr-worker@*'
journalctl --user -u ai-centre-ocr-gateway -f
journalctl --user -u 'ai-centre-ocr-worker@*' -f
```

Configuration lives in `config/linux`. Service definitions live in
`deploy/systemd-user`. The public video gateway should proxy this internal
service only after applying its normal authentication policy.
