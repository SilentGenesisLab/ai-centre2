# AI Centre 2 外部 API 接口文档

文档版本：2.0.0  
导出日期：2026-08-13  
生产环境：`https://aicentre2.sligenai.cn:8443`

> 本文档不保存生产密钥。请向管理员申请 Service Token，并通过环境变量注入。

## 1. 认证信息

除 `GET /health` 外，所有接口都需要 Bearer Token。

生产 API Key：

```text
<SERVICE_TOKEN>
```

HTTP 请求头：

```http
Authorization: Bearer <SERVICE_TOKEN>
Content-Type: application/json
```

Linux/macOS 建议先设置变量：

```bash
export AI_CENTRE2_BASE_URL="https://aicentre2.sligenai.cn:8443"
export AI_CENTRE2_API_KEY="<SERVICE_TOKEN>"
```

PowerShell：

```powershell
$BaseUrl = "https://aicentre2.sligenai.cn:8443"
$ApiKey = "<SERVICE_TOKEN>"
$Headers = @{ Authorization = "Bearer $ApiKey" }
```

## 2. 接口总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 中台和上游服务健康状态 |
| POST | `/v1/asr/transcriptions` | 通过音频或视频 URL 识别文字 |
| POST | `/v1/ocr/batch` | 通过图片 URL 批量 OCR |
| POST | `/v1/lipsync/jobs` | 创建唇形驱动任务 |
| GET | `/v1/lipsync/jobs` | 查询唇形任务列表 |
| GET | `/v1/lipsync/jobs/{job_id}` | 查询唇形任务状态 |
| GET | `/v1/lipsync/jobs/{job_id}/video` | 下载唇形结果视频 |
| GET | `/v1/lipsync/jobs/{job_id}/logs` | 查询唇形任务日志 |
| POST | `/v1/lipsync/jobs/{job_id}/cancel` | 取消唇形任务 |
| POST | `/v1/face-mosaic/jobs` | 创建异步人脸处理任务 |
| POST | `/v1/face-mosaic/jobs/wait` | 创建人脸任务并等待结果 |
| GET | `/v1/face-mosaic/jobs/{job_id}` | 查询人脸任务状态 |
| POST | `/v1/face-mosaic/jobs/{job_id}/cancel` | 取消人脸任务 |
| POST | `/v1/video-scenes/jobs` | 创建异步视频切片任务 |
| POST | `/v1/video-scenes/jobs/wait` | 创建视频切片任务并等待结果 |
| GET | `/v1/video-scenes/jobs/{job_id}` | 查询视频切片任务 |
| POST | `/v1/video-scenes/jobs/{job_id}/cancel` | 取消视频切片任务 |
| POST | `/v2/tts/speech` | 同步语音合成或语音克隆 |
| POST | `/v2/tts/speech/stream` | 流式语音合成 |
| GET | `/v2/tts/quality/{request_id}` | 查询异步质量审计 |
| POST | `/v2/tts/jobs` | 创建异步 TTS 任务 |
| GET | `/v2/tts/jobs/{job_id}` | 查询异步 TTS 状态 |
| GET | `/v2/tts/jobs/{job_id}/audio` | 下载异步 TTS 音频 |
| GET | `/v2/tts/providers` | 查询 TTS 服务商状态 |
| GET | `/v2/tts/voices` | 查询可用音色 |

## 3. 健康检查

不需要 API Key。

```bash
curl "$AI_CENTRE2_BASE_URL/health"
```

## 4. ASR 语音识别

### `POST /v1/asr/transcriptions`

请求字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `file_url` | 是 | 服务器能够访问的音频或视频 URL |
| `language` | 否 | 语言代码，默认 `auto` |
| `beam_size` | 否 | 1～10，默认 5 |

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v1/asr/transcriptions" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "file_url": "https://example.com/audio/demo.mp3",
    "language": "auto",
    "beam_size": 5
  }'
```

## 5. OCR 图片文字识别

### `POST /v1/ocr/batch`

支持单次 1～20 张图片。图片必须是服务端可以直接下载的 URL。

请求字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `job_id` | 否 | 调用方任务标识 |
| `source_lang_hint` | 否 | 源语言，如 `th`、`zh`、`en`、`es` |
| `images` | 是 | 图片数组，最多 20 张 |
| `images[].image_id` | 是 | 图片标识 |
| `images[].url` | 是 | 公网可访问图片 URL |
| `images[].time` | 否 | 视频帧时间戳 |
| `images[].regions` | 否 | 指定识别区域；空数组表示整图 |
| `regions[].bbox` | 否 | `[x1, y1, x2, y2]` |

### 泰语 PaddleOCR V5

必须且只能出现一次：

```json
"source_lang_hint": "th"
```

泰语请求使用：

- 文本检测：`PP-OCRv6_medium_det`
- 文本识别：`th_PP-OCRv5_mobile_rec`

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v1/ocr/batch" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "job_id": "thai-ocr-001",
    "source_lang_hint": "th",
    "images": [
      {
        "image_id": "front",
        "url": "https://example.com/images/thai.png",
        "regions": []
      }
    ]
  }'
```

成功响应中的实际识别模型应为：

```json
{
  "engine": "paddleocr",
  "model_version": "th_PP-OCRv5_mobile_rec",
  "results": []
}
```

中文、英文、西班牙语等默认使用 `PP-OCRv6_medium_rec`：

```json
{
  "job_id": "chinese-ocr-001",
  "source_lang_hint": "zh",
  "images": [
    {
      "image_id": "front",
      "url": "https://example.com/images/chinese.png",
      "regions": []
    }
  ]
}
```

> 不要在同一个 JSON 中重复填写 `source_lang_hint`。多数 JSON 解析器会使用最后一个值，例如前面写 `th`、末尾又写 `zh`，最终会按 `zh` 走 V6。

## 6. 唇形驱动

### 创建任务

`POST /v1/lipsync/jobs`

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v1/lipsync/jobs" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "video_url": "https://example.com/video/input.mp4",
    "audio_url": "https://example.com/audio/speech.mp3",
    "face_restore": true
  }'
```

### 查询任务

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/lipsync/jobs/JOB_ID"
```

任务列表支持 `limit` 和 `state` 查询参数：

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/lipsync/jobs?limit=20&state=succeeded"
```

### 下载结果

```bash
curl -L -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/lipsync/jobs/JOB_ID/video" \
  -o result.mp4
```

### 日志与取消

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/lipsync/jobs/JOB_ID/logs?stage=musetalk&tail=200"

curl -X POST -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/lipsync/jobs/JOB_ID/cancel"
```

`stage` 支持 `musetalk`、`gfpgan`。

## 7. 人脸处理

### 异步任务

`POST /v1/face-mosaic/jobs`

### 提交并等待结果

`POST /v1/face-mosaic/jobs/wait`

两个接口使用相同请求体：

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v1/face-mosaic/jobs/wait" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "source_uri": "https://example.com/video/input.mp4",
    "filename": "face_mosaic.mp4",
    "external_ref": "order-001",
    "metadata": {}
  }'
```

状态和取消：

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/face-mosaic/jobs/JOB_ID"

curl -X POST -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/face-mosaic/jobs/JOB_ID/cancel"
```

## 8. 视频场景切片

### 异步任务

`POST /v1/video-scenes/jobs`

### 提交并等待结果

`POST /v1/video-scenes/jobs/wait`

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v1/video-scenes/jobs/wait" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "source_uri": "https://example.com/video/input.mp4",
    "filename": "scene.mp4",
    "threshold": 27.0,
    "min_scene_len": 15,
    "external_ref": "order-001",
    "metadata": {}
  }'
```

状态和取消：

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/video-scenes/jobs/JOB_ID"

curl -X POST -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v1/video-scenes/jobs/JOB_ID/cancel"
```

## 9. TTS 语音合成与语音克隆

### 三条路，先选一条

| 想要 | 用什么 | 关键字段 |
|---|---|---|
| 用**已有音色**把文字念出来 | 9.1 | `text` + `voice_profile_id` |
| 用**一段话指挥怎么念**（情绪/停连/重音） | 9.2 | `emotion`（+ `emotion_enhance`） |
| 用**一段参考音频**复刻这个人的声音 | 9.3 | `reference_audio_url` + `prompt_text` |

三条都打 `POST /v2/tts/speech`，只是多带不同字段。

### 9.0 音色表是什么

`GET /v2/tts/voices` 列出可用的「声音档案」。一个档案 = 一个音色 ID + 它到各厂商音色的映射：

```json
{
  "voice_profile_id": "zh-female-news",
  "display_name": "中文 · 女 · 新闻播报",
  "languages": ["zh-CN"],
  "bindings": {
    "minimax": {"voice_id": "Chinese (Mandarin)_News_Anchor"},
    "voxcpm2": {"reference_audio": "/home/donxu/voices/zh-female-news.wav",
                "reference_text": "参考音频里说出的原文"}
  },
  "fallback_order": ["voxcpm2", "minimax"]
}
```

**音色列表本质上就是一批预先克隆好的语音**：`voxcpm2.reference_audio` 指向那一段参考音频，
选它就等于每次拿这段音频做克隆；`minimax.voice_id` 是厂商自己的系统音色。
用音色表的好处是不用每次上传参考音频、也不用每次跑 ASR。

当前（2026-11-01）只有 `default` 一条。想按名字点 200 个中英文音色，需要把档案铺起来，
铺的来源与做法见运维文档 `docs/TTS_PROVIDER_API.md` / 脚本 `scripts/import_tts_voice_catalog.py`。

### 9.1 普通合成（选音色）

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v2/tts/speech" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "text": "这是一段语音合成测试。",
    "language": "zh",
    "voice_profile_id": "default",
    "provider": "auto",
    "audio": {"format": "wav", "sample_rate": 48000, "channels": 1},
    "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0}
  }' \
  -o speech.wav
```

`voice_profile_id` 必须是 `GET /v2/tts/voices` 里存在的，否则 404。
`provider`：`auto`、`voxcpm2`、`minimax`、`doubao`、`elevenlabs`。

### 9.2 prompt 合成（指挥怎么念）

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v2/tts/speech" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "text": "这个价格，我真的没想到。",
    "voice_profile_id": "default",
    "emotion": "先压住语气，到「没想到」突然扬起，重读「真的」，句尾留半拍停顿",
    "emotion_enhance": false
  }' \
  -o styled.wav
```

`emotion` 最多 200 字；`emotion_enhance=true` 会先把这段要求扩写成结构化表演方案
（需要豆包大模型 key，没配会自动降级，响应头 `X-TTS-Emotion-Enhancement` 会写明）。
**它管「怎么念」，不管「谁的声音」**。

### 9.3 参考音频克隆

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v2/tts/speech" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "text": "欢迎体验我们的新产品。",
    "voice_profile_id": "default",
    "reference_audio_url": "https://example.com/audio/reference.wav",
    "prompt_text": "参考音频中准确说出的文本",
    "clone_mode": "auto",
    "emotion_strategy": "inherit"
  }' \
  -o cloned.wav
```

- `reference_audio_url` 必须是**公网 HTTPS 直链**；带它就启用深度克隆。
- `prompt_text` 是参考音频的原文；留空会先自动 ASR（更慢，且识别错会拖低相似度）。
- `clone_mode`：`auto`、`controllable`、`ultimate`。
- `emotion_strategy`：`auto`、`inherit`、`force`。
- `quality_mode`：`standard`、`strict`（`strict` 走三道候选 + 质量门禁，更慢）。

### 9.4 流式与异步

- 流式：把同一请求体发到 `POST /v2/tts/speech/stream`，响应是 48kHz/16bit/单声道 PCM。
- 长文本：`POST /v2/tts/jobs`，正文最多 20000 字符，`idempotency_key` 必填（8～256 字符）；
  用 `GET /v2/tts/jobs/{job_id}` 查状态、`GET /v2/tts/jobs/{job_id}/audio` 下载。

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v2/tts/jobs" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "text": "异步语音合成测试。",
    "language": "zh",
    "voice_profile_id": "default",
    "provider": "auto",
    "idempotency_key": "order-001-tts"
  }'

curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/jobs/JOB_ID"

curl -L -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/jobs/JOB_ID/audio" -o speech.wav
```

### 9.5 服务商、音色与质量审计

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" "$AI_CENTRE2_BASE_URL/v2/tts/providers"
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" "$AI_CENTRE2_BASE_URL/v2/tts/voices"
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" "$AI_CENTRE2_BASE_URL/v2/tts/quality/REQUEST_ID"
```

## 10. 常见状态码

| 状态码 | 含义 |
|---:|---|
| 200 | 请求成功 |
| 202 | 异步任务已接受 |
| 400 | 请求参数或业务条件错误 |
| 401 | API Key 无效或未提供 Bearer Token |
| 404 | 任务、资源或输入文件不存在 |
| 413 | 文件或批次数量超过限制 |
| 422 | JSON 字段格式或参数校验失败 |
| 500 | 服务内部错误 |
| 502 | 上游服务返回异常 |
| 503 | 服务或模型暂不可用 |
| 504 | 等待任务超时，后台任务可能仍在执行 |

## 11. Apifox/Postman 导入

将同目录下的 `ai-centre2-openapi.json` 导入 Apifox 或 Postman。

导入后设置环境变量：

```text
baseUrl = https://aicentre2.sligenai.cn:8443
apiKey  = 本文档第 1 节中的生产 API Key
```

认证方式选择 Bearer Token。不要把 `Bearer ` 写进 API Key 变量本身。
