# Repository layout

```text
ai-centre/
├── control_plane/       ASR/TTS gateway and GPU lifecycle API
├── face_worker/         face mosaic API and Celery worker
├── OCR/                 OCR gateway and GPU workers
├── deploy/              systemd units and backend configurations
├── docs/                architecture and operations documentation
├── scripts/             installation and verification commands
├── tests/               control-plane tests
├── data/                runtime jobs; ignored by Git
├── models/              model weights; ignored by Git
└── runtime/             logs, state and caches; ignored by Git
```

The existing `face_worker` and `OCR` import paths stay unchanged so that the
currently deployed services do not break during migration.

