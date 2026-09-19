x# VoxCPM2 vLLM-Omni v0.26.0 production validation

Validated on 2026-08-03 on physical GPU 1, an NVIDIA GeForce RTX 5090.

## Deployed versions

- Python 3.12.13
- PyTorch 2.11.0+cu130
- vLLM 0.26.0
- vLLM-Omni 0.26.0
- VoxCPM 2.0.3

The production service remains `ai-centre-voxcpm2-gpu1.service` and listens on
`127.0.0.1:8192`. The legacy compatibility gateway remains on
`127.0.0.1:8193`, so no control-plane or public API route changed.

The v0.26 environment is isolated at `/home/donxu/ai-centre/.venv-tts-v026`.
The previous environment and its service backup were retained for rollback.

## Production profile

The profile in `deploy/voxcpm2-v026.yaml` is based on the upstream v0.26.0
VoxCPM2 profile with limits for the shared GPU:

- `enforce_eager: true`
- maximum four sequences
- 4 GiB KV cache
- unified decode graph maximum batch size four
- FlashInfer sampler disabled because the host lacks a system C++ compiler

This raised steady-state GPU headroom from about 2.2 GiB to about 4.35 GiB.

## Quality results

All generated audio was transcribed by the managed Faster-Whisper service.

| Test | Result | Character error rate | Duration / latency |
| --- | --- | ---: | --- |
| Plain Chinese | Passed | 0% | 5.44 s audio |
| Chinese voice clone | Passed | 0% | 4.16 s audio |
| Four requests at concurrency 2 | Passed | 0% each | 0.42-0.66 s after lazy warmup |
| Four requests at concurrency 4 | Passed | 0-5.26% | 0.54-0.65 s |
| Production profile, concurrency 4 repeat | Passed | 0% each | 0.50-0.58 s |
| Public `POST /v2/tts/speech` | Passed | Semantic match | HTTP 200 |

The one 5.26% result was an ASR notation difference: the spoken Chinese
numeral `三` was transcribed as `3`. No request contained text from another
request, and no unrelated vocalization or long hallucinated tail was detected.

Validation artifacts and JSON reports are stored under
`runtime/validation/voxcpm2-v026*` on the server.

## Verification and rollback

```bash
systemctl --user status ai-centre-voxcpm2-gpu1.service
curl -fsS http://127.0.0.1:8192/health
curl -fsS http://127.0.0.1:8193/health
```

The pre-upgrade unit is stored at:

```text
/home/donxu/ai-centre/runtime/control/service-backups/
ai-centre-voxcpm2-gpu1.service.before-v026-20260803T1618Z
```

To roll back, copy that unit over the user unit, reload systemd, and restart the
service. The old `.venv-tts` environment was not removed.

## 2026-08-17 short-audio stability release

Production now uses the reproducible internal wheel
`vllm_omni-0.26.0+sligen1-py3-none-any.whl`. The patch adds the official
`text token count * 6 + 10` decode ceiling, a four-step minimum stop guard, and
caller-seed plus decode-step CFM noise. The original PyPI wheel and the patched
wheel are both retained in `/home/donxu/ai-centre/vendor/wheels`.

The control plane additionally uses natural-pause reference windows, 500 ms
reference-tail silence, synchronous short-audio quality gates, generation-error
retry, exact-content gates for text up to 20 characters, abnormal head/tail
detection, safe tail trimming, and finalized WAV headers. Reference-window,
reference-emotion, and speaker-similarity caches retain only hashes and numeric
metadata; signed URLs, text, and media bytes are not cached.

Production validation reports are under
`/home/donxu/ai-centre/runtime/validation/voxcpm2-sligen1`:

| Public API case | Requests | Exact content | Extra speech | P95 | Duration P99 / median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cross-language clone, single concurrency | 100 | 100% | 0 | 8.11 s before metadata caches | 1.00 |
| Cross-language clone, hot single sample | 20 | 100% | 0 | 1.78 s | 1.00 |
| Cross-language clone, concurrency 4 | 100 | 100% | 0 | 8.78 s | 1.00 |
| One Chinese character | 100 | 100% | 0 | 0.16 s | 1.00 |
| Four Chinese characters | 100 | 100% | 0 | 0.23 s | 1.00 |
| Special punctuation and safe tail trim | 100 | 100% | 0 | 0.82 s | 1.00 |

The concurrency-4 latency tail is caused by the single-threaded CPU CAM++
quality verifier, not GPU generation or output-length instability. All 100
concurrent requests passed content, edge, duration, container, and quality
checks; future latency work can move CAM++ to a worker pool without changing
the public API.

Wheel-only rollback:

```bash
/home/donxu/ai-centre/.venv-tts-v026/bin/python -m pip install \
  --no-deps --force-reinstall \
  /home/donxu/ai-centre/vendor/wheels/vllm_omni-0.26.0-py3-none-any.whl
systemctl --user restart ai-centre-voxcpm2-gpu1.service
```
