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

### 同步合成

`POST /v2/tts/speech`

```bash
curl -X POST "$AI_CENTRE2_BASE_URL/v2/tts/speech" \
  -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "text": "这是一段语音合成测试。",
    "language": "zh",
    "voice_profile_id": "default",
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
    "quality_mode": "standard"
  }' \
  -o speech.wav
```

`provider` 支持：`auto`、`voxcpm2`、`doubao`、`elevenlabs`。

### 参考音频克隆

在同步请求中增加：

```json
{
  "reference_audio_url": "https://example.com/audio/reference.wav",
  "prompt_text": "参考音频中准确说出的文本",
  "clone_mode": "auto",
  "emotion_strategy": "inherit"
}
```

`clone_mode`：`auto`、`controllable`、`ultimate`。  
`emotion_strategy`：`auto`、`inherit`、`force`。  
`quality_mode`：`standard`、`strict`。

### 流式合成

将同一请求体发送到：

```text
POST /v2/tts/speech/stream
```

### 异步 TTS

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
```

查询和下载：

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/jobs/JOB_ID"

curl -L -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/jobs/JOB_ID/audio" \
  -o speech.wav
```

### 服务商、音色和质量审计

```bash
curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/providers"

curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/voices"

curl -H "Authorization: Bearer $AI_CENTRE2_API_KEY" \
  "$AI_CENTRE2_BASE_URL/v2/tts/quality/REQUEST_ID"
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
