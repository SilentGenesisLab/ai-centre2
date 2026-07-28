# 统一 TTS Provider API

AI Centre 2 用一个稳定的 V2 契约封装本地 VoxCPM2、火山引擎豆包 TTS 和
ElevenLabs。业务系统只依赖 `voice_profile_id`，不直接保存第三方音色 ID、
密钥或厂商请求格式。

## 1. 架构

```text
业务请求
   |
   +--> POST /v2/tts/speech --------> TTSService
   |                                    |
   +--> POST /v2/tts/jobs --> Celery -->+--> VoiceRegistry
                                        +--> Provider Router
                                              |- VoxCPM2
                                              |- Doubao
                                              `- ElevenLabs
                                        +--> FFmpeg canonical WAV
                                        `--> result metadata/audio
```

- 对外固定输出：WAV、16-bit PCM，默认 48 kHz、单声道。
- 同步接口适合调试和低并发；批量视频必须优先使用异步任务接口。
- 异步任务使用 Redis/Celery，`idempotency_key` 防止重复生成和重复计费。
- 自动路由只在瞬时错误时切换 provider。音色不存在、参数错误等永久错误不会
  偷偷换成另一种声音。
- `target_duration_ms` 只用于报告时长偏差，不会硬性拉伸到损坏自然语速。

## 2. 鉴权与地址

当前公网入口：

```text
http://aicentre2.sligenai.cn:8320
```

除健康检查和 OpenAPI 文档外，请求必须携带：

```http
Authorization: Bearer <SERVICE_TOKEN>
```

Token 保存在服务器 `/home/donxu/ai-centre/.env`，不得写入 Git、前端或日志。

## 3. Provider 配置

配置文件为 `/home/donxu/ai-centre/.env`。

```dotenv
TTS_VOICE_REGISTRY_PATH=/home/donxu/ai-centre/runtime/control/tts-voices.json
TTS_OUTPUT_DIR=/home/donxu/ai-centre/runtime/control/tts-output
TTS_FFMPEG_BIN=auto
TTS_JOB_WORKERS=8
TTS_RESULT_EXPIRES_SECONDS=604800
TTS_AUTO_PROVIDER_ORDER=voxcpm2,doubao,elevenlabs

VOXCPM2_TTS_ENABLED=true
VOXCPM2_TTS_MAX_CONCURRENCY=4

DOUBAO_TTS_ENABLED=false
DOUBAO_TTS_URL=https://openspeech.bytedance.com/api/v1/tts
DOUBAO_TTS_APP_ID=
DOUBAO_TTS_ACCESS_TOKEN=
DOUBAO_TTS_CLUSTER=volcano_tts
DOUBAO_TTS_MAX_CONCURRENCY=4

ELEVENLABS_TTS_ENABLED=false
ELEVENLABS_TTS_BASE_URL=https://api.elevenlabs.io
ELEVENLABS_TTS_API_KEY=
ELEVENLABS_TTS_MODEL_ID=eleven_multilingual_v2
ELEVENLABS_TTS_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_TTS_MAX_CONCURRENCY=4
```

变更 `.env` 后重启：

```bash
systemctl --user restart ai-centre-control.service
systemctl --user restart ai-centre-tts-worker.service
```

查看状态：

```bash
systemctl --user status ai-centre-control.service
systemctl --user status ai-centre-tts-worker.service
journalctl --user -u ai-centre-tts-worker.service -n 100 --no-pager
```

## 4. 声音档案

声音档案把业务声音 ID 映射到各厂商声音 ID。示例：

```json
{
  "voice_profile_id": "female-ad-th-v1",
  "display_name": "Thai female advertisement",
  "languages": ["th-TH"],
  "bindings": {
    "voxcpm2": {
      "reference_audio": "/home/donxu/voices/female-ad.wav",
      "reference_text": "参考音频对应文本"
    },
    "doubao": {
      "voice_type": "S_DQ..."
    },
    "elevenlabs": {
      "voice_id": "xxxxxxxxxxxxxxxxxxxx",
      "model_id": "eleven_multilingual_v2",
      "voice_settings": {
        "stability": 0.45,
        "similarity_boost": 0.8
      }
    }
  },
  "fallback_order": ["voxcpm2", "doubao", "elevenlabs"],
  "version": 1
}
```

写入或更新声音档案：

```bash
curl -X PUT \
  "http://aicentre2.sligenai.cn:8320/v2/tts/voices/female-ad-th-v1" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @voice-profile.json
```

服务端会原子写入 Registry。更新已有档案时，版本号会自动递增。

查询声音：

```bash
curl \
  "http://aicentre2.sligenai.cn:8320/v2/tts/voices" \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

## 5. 同步生成

```bash
curl -X POST \
  "http://aicentre2.sligenai.cn:8320/v2/tts/speech" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -o speech.wav \
  -D response-headers.txt \
  -d '{
    "text": "ผิวของคุณจะดูอ่อนเยาว์ขึ้น",
    "language": "th-TH",
    "voice_profile_id": "female-ad-th-v1",
    "provider": "auto",
    "audio": {
      "format": "wav",
      "sample_rate": 48000,
      "channels": 1
    },
    "prosody": {
      "speed": 1.0,
      "volume": 1.0,
      "pitch": 1.0
    },
    "timing": {
      "target_duration_ms": 2400,
      "tolerance_ms": 200
    },
    "metadata": {
      "video_id": "video-001",
      "segment_id": "segment-003"
    }
  }'
```

响应头包括 `X-TTS-Provider`、`X-Audio-Duration-Ms` 和可选的
`X-Provider-Request-Id`。

`provider` 可取 `auto`、`voxcpm2`、`doubao`、`elevenlabs`。

## 6. 异步批量生成

提交：

```bash
curl -X POST \
  "http://aicentre2.sligenai.cn:8320/v2/tts/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Nunca es tarde para cuidar tu piel.",
    "language": "es-MX",
    "voice_profile_id": "female-ad-es-v1",
    "provider": "auto",
    "idempotency_key": "video-001:segment-003:translation-v2",
    "timing": {
      "target_duration_ms": 3100,
      "tolerance_ms": 250
    }
  }'
```

接受响应：

```json
{
  "job_id": "tts_...",
  "status": "queued",
  "duplicate": false
}
```

使用相同 `idempotency_key` 重复提交会返回同一个 `job_id`，并显示
`"duplicate": true`。

查询：

```bash
curl \
  "http://aicentre2.sligenai.cn:8320/v2/tts/jobs/<job_id>" \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

下载：

```bash
curl \
  "http://aicentre2.sligenai.cn:8320/v2/tts/jobs/<job_id>/audio" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -o segment.wav
```

状态可能为 `queued`、`running`、`succeeded`、`failed`。任务未完成时下载接口
返回 HTTP 409。

## 7. Provider 状态

```bash
curl \
  "http://aicentre2.sligenai.cn:8320/v2/tts/providers" \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

该接口说明 provider 是否启用和是否配置，不替代真实合成探针。生产监控应同时
提交一条短文本的周期性探针，并分别统计：

- API 排队耗时；
- provider 首包/总耗时；
- 音频标准化耗时；
- 成功率、重试率、fallback 率；
- 实际音频时长与目标时长偏差。

## 8. 错误语义

- `401`：缺少或错误的服务 Token。
- `404`：声音档案或任务不存在。
- `409`：异步音频尚未生成完成。
- `422`：永久错误，例如声音绑定缺失、厂商参数错误。
- `502`：provider 返回未分类错误。
- `503`：瞬时上游故障、超时或任务队列不可用。

## 9. 与 ASR 的边界

Whisper/faster-whisper 是 ASR，不是 TTS：

```text
原音频 -> ASR -> 带时间戳文本 -> 翻译 -> 分段/时长规划
       -> 统一 TTS -> 标准 WAV -> 对齐/混音 -> 成片
```

不要把 ASR 模型注册成 TTS provider。二者可以共享任务元数据和追踪 ID，但
应拥有独立队列、并发限制、健康检查和 GPU 生命周期。

