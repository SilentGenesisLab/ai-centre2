# AI Centre 2 对外 API 使用手册

版本：3.1  
更新日期：2026-09-22  
生产地址：`https://aicentre2.sligenai.cn:8443`

> 本文档仅列出当前生产环境已经开放的公网接口。复制示例后，替换 `API_KEY` 和素材 URL 即可调用。

## 1. 五分钟接入

### 1.1 准备 API Key

API Key 由 AI Centre 管理员单独分配。公开文档不会包含真实密钥。

所有业务接口都使用以下请求头：

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

`Bearer` 后必须有一个空格：

```text
正确：Authorization: Bearer aic_xxxxxxxxx
错误：Authorization: aic_xxxxxxxxx
```

Linux/macOS：

```bash
export BASE_URL='https://aicentre2.sligenai.cn:8443'
export API_KEY='替换为管理员分配的API Key'
```

Windows PowerShell：

```powershell
$BaseUrl = 'https://aicentre2.sligenai.cn:8443'
$ApiKey = '替换为管理员分配的API Key'
$Headers = @{ Authorization = "Bearer $ApiKey" }
```

API Key 只能放在调用方服务端。不要写入浏览器前端、App安装包、Git仓库、日志或截图。

### 1.2 检查服务

健康检查不需要 API Key：

```bash
curl -sS "$BASE_URL/health"
```

### 1.3 素材 URL 规则

- 公网接口只接受公网 `HTTPS` URL，不接受 `C:\...`、`K:\...`、`/home/...` 等本地路径。
- 本地素材可以先用中台的 `/v1/uploads` 上传换取直链，见 §1.4；也可以自带带签名查询参数的 OSS/S3 URL。
- 禁止 localhost、内网地址、云元数据地址、URL 用户名密码和非 HTTPS 协议。
- 重定向后的每个地址都会重新进行安全检查。
- 音视频通常最大 512 MiB；OCR 单图最大 20 MiB、单批最多20张。
- 签名 URL 只需在中台下载素材期间有效，建议至少保留30分钟有效期。

### 1.4 上传本地素材

本地文件不必先传到外部对象存储。中台自带上传接口，上传成功后返回的公网 HTTPS 直链，
可以直接填进任何接口的 `*_url` / `source_uri` 字段。

```bash
curl -sS -X POST "$BASE_URL/v1/uploads" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@./key-visual.png"
```

返回 `201`：

```json
{
  "key": "ai-centre/uploads/2026/09/22/fff270c6d88247eca9cfbe41006920bf.png",
  "url": "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/ai-centre/uploads/2026/09/22/fff270c6d88247eca9cfbe41006920bf.png",
  "content_type": "image/png",
  "bytes": 12335,
  "filename": "key-visual.png"
}
```

- 请求为 `multipart/form-data`，字段名固定为 `file`，单文件最大 512 MiB。
- 空文件返回 `422`；中台未配置对象存储时返回 `503`。
- 前缀由服务端决定，调用方不能指定；每次上传生成新的随机文件名，不会覆盖已有对象。
- 返回的 `url` 是对象的公网直链，无需再签名，可直接使用。
- 上传的素材同样受 §11 的保密要求约束：不要放进浏览器前端或公开仓库。

## 2. 接口总览

### 2.1 生文与文本能力

| 中文接口名 | 方法 | 路径 | 返回方式 |
|---|---|---|---|
| 音视频转文字 | POST | `/v1/asr/transcriptions` | 同步 JSON |
| 图片批量文字识别 | POST | `/v1/ocr/batch` | 同步 JSON |

当前生产环境尚未开放通用大语言模型“提示词生成文章/对话”接口。ASR 和 OCR 是当前可对外使用的文本输出能力。

### 2.2 生图

| 中文接口名 | 方法 | 路径 | 返回方式 |
|---|---|---|---|
| 创建图像生成任务 | POST | `/v1/image-generations/jobs` | 异步任务 |
| 查询图像生成任务 | GET | `/v1/image-generations/jobs/{job_id}` | JSON |
| 取消图像生成任务 | POST | `/v1/image-generations/jobs/{job_id}/cancel` | JSON |

### 2.3 生视频

| 中文接口名 | 方法 | 路径 | 返回方式 |
|---|---|---|---|
| 通用视频生成 | POST | `/v1/video-generations/jobs` | 异步任务 |
| 查询通用视频任务 | GET | `/v1/video-generations/jobs/{job_id}` | JSON |
| 取消通用视频任务 | POST | `/v1/video-generations/jobs/{job_id}/cancel` | JSON |
| MiniMax H3 视频生成 | POST | `/v1/video-generations/minimax-h3/jobs` | 异步任务 |
| 查询 H3 视频任务 | GET | `/v1/video-generations/minimax-h3/jobs/{job_id}` | JSON |
| 查询 H3 排队位置 | GET | `/v1/video-generations/minimax-h3/jobs/{job_id}/queue-position` | JSON |
| 查询 H3 Worker 容量 | GET | `/v1/video-generations/minimax-h3/workers/status` | JSON |
| 取消 H3 视频任务 | POST | `/v1/video-generations/minimax-h3/jobs/{job_id}/cancel` | JSON |

### 2.4 生音乐与音效

| 中文接口名 | 方法 | 路径 | 返回方式 |
|---|---|---|---|
| 创建音乐/音效生成任务 | POST | `/v1/audio-generations/jobs` | 异步任务 |
| 查询音乐/音效生成任务 | GET | `/v1/audio-generations/jobs/{job_id}` | JSON |
| 取消音乐/音效生成任务 | POST | `/v1/audio-generations/jobs/{job_id}/cancel` | JSON |

**一次生成产出两首成品**（上游返回两条 task），`result_urls` 里有两条 mp3。详见第 6 节。

### 2.5 语音能力

| 中文接口名 | 方法 | 路径 | 返回方式 |
|---|---|---|---|
| 同步语音合成/克隆 | POST | `/v2/tts/speech` | WAV 文件 |
| 实时流式语音合成 | POST | `/v2/tts/speech/stream` | 48kHz PCM 流 |
| 创建长文本语音任务 | POST | `/v2/tts/jobs` | 异步任务 |
| 查询长文本语音任务 | GET | `/v2/tts/jobs/{job_id}` | JSON |
| 下载长文本语音 | GET | `/v2/tts/jobs/{job_id}/audio` | WAV 文件 |
| 查询语音质量审计 | GET | `/v2/tts/quality/{request_id}` | JSON |
| 查询可用音色 | GET | `/v2/tts/voices` | JSON |
| 查询语音服务状态 | GET | `/v2/tts/providers` | JSON |

### 2.6 视频、音频与图像处理

| 中文接口名 | 创建异步任务 | 创建并等待 | 查询任务 | 取消任务 |
|---|---|---|---|---|
| 唇形驱动/GFPGAN | `POST /v1/lipsync/jobs` | — | `GET /v1/lipsync/jobs/{job_id}` | `POST /v1/lipsync/jobs/{job_id}/cancel` |
| 人脸处理 | `POST /v1/face-mosaic/jobs` | `POST /v1/face-mosaic/jobs/wait` | `GET /v1/face-mosaic/jobs/{job_id}` | `POST /v1/face-mosaic/jobs/{job_id}/cancel` |
| 视频场景切片 | `POST /v1/video-scenes/jobs` | `POST /v1/video-scenes/jobs/wait` | `GET /v1/video-scenes/jobs/{job_id}` | `POST /v1/video-scenes/jobs/{job_id}/cancel` |
| AI 视频拉片/拆审 | `POST /v1/video-reviews/jobs` | — | `GET /v1/video-reviews/jobs/{job_id}` | `POST /v1/video-reviews/jobs/{job_id}/cancel` |
| 视频深度推理 | `POST /v1/video-depth/jobs` | `POST /v1/video-depth/jobs/wait` | `GET /v1/video-depth/jobs/{job_id}` | `POST /v1/video-depth/jobs/{job_id}/cancel` |
| 视频超分 | `POST /v1/video-upscale/jobs` | `POST /v1/video-upscale/jobs/wait` | `GET /v1/video-upscale/jobs/{job_id}` | `POST /v1/video-upscale/jobs/{job_id}/cancel` |
| 音频四轨分离 | `POST /v1/audio-separation/jobs` | `POST /v1/audio-separation/jobs/wait` | `GET /v1/audio-separation/jobs/{job_id}` | `POST /v1/audio-separation/jobs/{job_id}/cancel` |
| 授权视频水印处理 | `POST /v1/watermark-removal/jobs` | `POST /v1/watermark-removal/jobs/wait` | `GET /v1/watermark-removal/jobs/{job_id}` | `POST /v1/watermark-removal/jobs/{job_id}/cancel` |

## 3. 生文与文本能力

### 3.1 音视频转文字

`POST /v1/asr/transcriptions`

```bash
curl -sS -X POST "$BASE_URL/v1/asr/transcriptions" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "file_url": "https://storage.example.com/audio/interview.MP3",
    "language": "auto",
    "beam_size": 5
  }'
```

字段：

| 字段 | 必填 | 范围 | 说明 |
|---|---:|---|---|
| `file_url` | 是 | 公网 HTTPS URL | 音频或含音轨的视频 |
| `language` | 否 | `auto`、`zh`、`en` 等 | 默认 `auto` |
| `beam_size` | 否 | 1～10 | 默认5；更大通常更慢 |

响应示例：

```json
{
  "language": "zh",
  "duration": 10.24,
  "text": "您好，这是语音识别示例。",
  "segments": [
    {"start": 0.0, "end": 2.6, "text": "您好，这是语音识别示例。"}
  ]
}
```

### 3.2 图片批量文字识别

`POST /v1/ocr/batch`

```bash
curl -sS -X POST "$BASE_URL/v1/ocr/batch" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "job_id": "ocr-order-001",
    "source_lang_hint": "zh",
    "images": [
      {
        "image_id": "front",
        "url": "https://storage.example.com/images/front.png",
        "regions": []
      },
      {
        "image_id": "back",
        "url": "https://storage.example.com/images/back.jpg",
        "regions": [{"name": "label", "bbox": [20, 30, 800, 600]}]
      }
    ]
  }'
```

`bbox` 为 `[x1, y1, x2, y2]` 像素坐标；`regions=[]` 表示识别整张图片。

## 4. 生图接口

### 4.1 创建图像生成任务

`POST /v1/image-generations/jobs`

纯文本生图：

```bash
curl -sS -X POST "$BASE_URL/v1/image-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "gpt-image-2.5",
    "channel": "grsai",
    "prompt": "浅蓝色科技风护肤精华产品广告，玻璃瓶，纯净水面，商业摄影，柔和轮廓光",
    "reference_image_urls": [],
    "aspect_ratio": "9:16",
    "image_size": "2K",
    "external_ref": "image-order-001",
    "metadata": {"campaign": "summer"}
  }'
```

参考图生图：

```bash
curl -sS -X POST "$BASE_URL/v1/image-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "nano-banana-2",
    "prompt": "保持参考产品外观，生成高端TVC产品英雄镜头，蓝色科技背景",
    "reference_image_urls": [
      "https://storage.example.com/images/product-front.png",
      "https://storage.example.com/images/product-side.png"
    ],
    "aspect_ratio": "16:9",
    "image_size": "2K"
  }'
```

参数：

| 字段 | 必填 | 可选值/限制 | 默认 |
|---|---:|---|---|
| `model` | 否 | `gpt-image-2`、`gpt-image-2.5`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、`nano-banana-2` | `gpt-image-2` |
| `channel` | 否 | `grsai` | `grsai` |
| `prompt` | 是 | 1～10000字符 | — |
| `reference_image_urls` | 否 | 最多9张公网 HTTPS 图片 | `[]` |
| `aspect_ratio` | 否 | `1:1`、`2:3`、`3:2`、`3:4`、`4:3`、`9:16`、`16:9` | `1:1` |
| `image_size` | 否 | `1K`、`2K`、`4K` | `1K` |
| `external_ref` | 否 | 最多256字符 | `null` |
| `metadata` | 否 | JSON对象 | `{}` |

提交成功返回 HTTP 202：

```json
{
  "job_id": "23f773f9-7e2b-4748-9387-79f1952c7be0",
  "status": "queued",
  "model": "gpt-image-2.5",
  "requested_channel": "grsai",
  "status_url": "/v1/image-generations/jobs/23f773f9-7e2b-4748-9387-79f1952c7be0"
}
```

### 4.2 查询与取消生图任务

```bash
JOB_ID='替换为提交返回的job_id'

curl -sS \
  -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/image-generations/jobs/$JOB_ID"

curl -sS -X POST \
  -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/image-generations/jobs/$JOB_ID/cancel"
```

完成时查询结果的 `status` 为 `succeeded`，最终图片在 `result_urls` 数组中。

## 5. 生视频接口

### 5.1 通用视频生成

`POST /v1/video-generations/jobs`

可同时传多张图片、多个视频和多段音频。具体支持数量取决于渠道与模型：`seedance-2.0` 是 `jmapi` 最多9图、3视频、3音频，`libtv` 最多9图、3视频且不接收独立音频；`seedance-2.5` 是两个渠道都最多30图、10视频（参考视频/音频总时长上限 30 秒），独立音频只有 `jmapi` 接收。

纯文本请求可以不传任何参考素材。由于上游 Seedance 渠道要求至少存在一个媒体节点，AI Centre 会在服务端自动注入一张中性空白参考图；调用方不需要准备空白图，任务记录也仍按“纯文本生成”统计。只要显式提供了任一图片、视频或音频，系统就不会注入空白图。

纯文本示例：

```bash
curl -sS -X POST "$BASE_URL/v1/video-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "seedance-2.0",
    "channel": "auto",
    "prompt": "清晨的现代城市街道，阳光穿过建筑，镜头平稳向前推进，写实商业广告质感",
    "duration_seconds": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "sound": false
  }'
```

```bash
curl -sS -X POST "$BASE_URL/v1/video-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "seedance-2.0",
    "channel": "jmapi",
    "prompt": "产品从黑暗中缓慢出现，镜头环绕，金属边缘被蓝色轮廓光照亮，商业TVC质感",
    "reference_image_urls": ["https://storage.example.com/images/product.png"],
    "reference_video_urls": [],
    "reference_audio_urls": [],
    "duration_seconds": 10,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "sound": false,
    "external_ref": "video-order-001",
    "metadata": {"campaign": "summer"}
  }'
```

参数：

| 字段 | 必填 | 可选值/限制 | 默认 |
|---|---:|---|---|
| `model` | 否 | `seedance-2.0`、`seedance-2.5` | `seedance-2.0` |
| `channel` | 否 | `jmapi`、`libtv`、`auto` | `jmapi` |
| `prompt` | 是 | 1～10000字符 | — |
| `reference_image_urls` | 否 | 2.0最多9张；2.5最多30张 | `[]` |
| `reference_video_urls` | 否 | 2.0最多3个；2.5最多10个 | `[]` |
| `reference_audio_urls` | 否 | jmapi最多3个（2.5最多10个）；libtv不支持 | `[]` |
| `duration_seconds` | 否 | 2～30秒；`seedance-2.0` 上限15秒 | 5 |
| `resolution` | 否 | `480p`、`720p`、`1080p`、`2K` | `720p` |
| `aspect_ratio` | 否 | `9:16`、`16:9`、`1:1`、`4:3`、`3:4` | `9:16` |
| `sound` | 否 | `true`保留上游音轨；`false`在OSS转存前确定性移除音轨 | `false` |
| `external_ref` | 否 | 最多256字符 | `null` |
| `metadata` | 否 | JSON对象 | `{}` |

渠道分辨率说明：当前 jmapi 的 `seedance2.0_vip` 不接受 `480p`，明确指定 `channel: "jmapi"` 且请求480P时接口会在提交前返回422。需要480P时请使用 `channel: "auto"`（自动选择 libtv）或明确指定 `libtv`；720P已在两个渠道完成生产验证。

### 5.1.1 Seedance 2.5

`model` 传 `seedance-2.5` 即可，两个渠道都提供：

| 渠道 | 上游模型 | 时长 | 分辨率 | 参考素材 |
|---|---|---|---|---|
| `jmapi` | `model_version=seedance2.5` | 4～30秒 | `480p`、`720p` | 最多30图、10视频、10音频；视频/音频**合计**时长 ≤30秒 |
| `libtv` | `star-video2.5` | 4～30秒 | `480p`、`720p`、`1080p` | 最多30图、10视频；不接收独立音频 |

- `480p` 只有 2.5 能做：jmapi 侧 2.0 会被上游拒绝，2.5 可以。
- `1080p` 在 jmapi 侧不可用（上游只认 480p/720p），`channel: "auto"` 会自动落到 libtv；显式指定 `jmapi` 时接口在提交前返回422。
- 时长超过 15 秒的请求只有 2.5 能接，显式指定 `seedance-2.0` 时同样在提交前返回422，不会把必然被上游拒绝的请求发出去。
- 上游还支持 `21:9` 和 `adaptive` 画幅，当前接口的 `aspect_ratio` 尚未开放这两个值。
- 结果与 2.0 一样先转存到 AI Centre 的 OSS 再返回 `result_urls`。

### 5.2 查询与取消通用视频任务

```bash
JOB_ID='替换为提交返回的job_id'

curl -sS \
  -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/video-generations/jobs/$JOB_ID"

curl -sS -X POST \
  -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/video-generations/jobs/$JOB_ID/cancel"
```

完成时查询结果的 `status` 为 `succeeded`，最终视频位于 `result_urls`。Seedance 结果会先转存到 AI Centre 的 OSS，再返回稳定的公网 HTTPS 地址，不返回上游临时 CDN 地址。

使用 `channel: "auto"` 时，提交前不兼容、未取得任务ID的拒绝，以及上游明确进入失败终态都会尝试下一可用渠道；超时或结果转存失败不会自动重复生成，以免产生两个远端成片和重复费用。

排队中的任务取消后立即变为 `cancelled`。已经提交给上游的任务会先返回 `cancel_requested`，AI Centre 停止轮询和发布结果后再变为 `cancelled`；由于 jmapi/libtv 当前没有可靠的上游取消接口，已经开始的上游生成仍可能继续消耗资源。

### 5.3 MiniMax H3 视频生成

`POST /v1/video-generations/minimax-h3/jobs`

H3 当前采用“一个请求生成一个原子镜头”。需要多段视频时由业务端提交多个任务并在后期拼接。

纯文本视频：

```bash
curl -sS -X POST "$BASE_URL/v1/video-generations/minimax-h3/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "prompt": "透明玻璃精华瓶立在水面中央，蓝色粒子从瓶底升起，镜头缓慢推进，商业TVC质感",
    "duration_seconds": 5,
    "quality": "medium",
    "resolution": "720p",
    "aspect_ratio": "9:16",
    "priority": 500,
    "seed": 482901731,
    "external_ref": "h3-order-001",
    "metadata": {}
  }'
```

多参考素材视频：

```bash
curl -sS -X POST "$BASE_URL/v1/video-generations/minimax-h3/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "reference_image_urls": [
      "https://storage.example.com/images/product-front.png",
      "https://storage.example.com/images/product-side.png"
    ],
    "reference_video_urls": ["https://storage.example.com/video/camera-motion.mp4"],
    "reference_audio_urls": ["https://storage.example.com/audio/music.mp3"],
    "prompt": "保持产品造型，继承参考视频的镜头运动和参考音频节奏，生成高端产品TVC镜头",
    "duration_seconds": 10,
    "quality": "medium",
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "priority": 500,
    "seed": 482901731
  }'
```

参数：

| 字段 | 必填 | 可选值/限制 | 默认 |
|---|---:|---|---|
| `prompt` | 是 | 1～20000字符；原样传给模型 | — |
| `reference_image_urls` | 否 | 最多8张 | `[]` |
| `reference_video_urls` | 否 | 最多8个 | `[]` |
| `reference_audio_urls` | 否 | 最多8个 | `[]` |
| `duration_seconds` | 否 | 2～15秒 | 5 |
| `quality` | 否 | `low`、`medium`、`high` | `medium` |
| `resolution` | 否 | `480p`、`720p`、`1080p` | `720p` |
| `aspect_ratio` | 否 | `9:16`、`16:9`、`1:1`、`4:3`、`3:4` | `9:16` |
| `width`/`height` | 否 | 32～1344且为32倍数；一般不要与预设分辨率混用 | 自动 |
| `priority` | 否 | 1～1000，数值越大越先分配空闲Worker；不会打断运行中任务 | 500 |
| `seed` | 否 | 非负整数 | 482901731 |
| `external_ref` | 否 | 最多256字符 | `null` |
| `metadata` | 否 | JSON对象 | `{}` |

提交返回：

```json
{
  "job_id": "f0a7de17-f6ef-4ddf-93ea-3eeec3731f21",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "priority": 500,
  "resolution": "720p",
  "quality": "medium",
  "aspect_ratio": "9:16",
  "status_url": "/v1/video-generations/minimax-h3/jobs/f0a7de17-f6ef-4ddf-93ea-3eeec3731f21",
  "queue_position_url": "/v1/video-generations/minimax-h3/jobs/f0a7de17-f6ef-4ddf-93ea-3eeec3731f21/queue-position"
}
```

查询任务、排队位置和 Worker 容量：

```bash
JOB_ID='替换为提交返回的job_id'

curl -sS -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/video-generations/minimax-h3/jobs/$JOB_ID"

curl -sS -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/video-generations/minimax-h3/jobs/$JOB_ID/queue-position"

curl -sS -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/video-generations/minimax-h3/workers/status"
```

成功任务的 `status` 为 `succeeded`，`result_url` 是可直接访问的 OSS 成片。

## 6. 音乐与音效生成

渠道：`mxapi`（Suno）。两种能力共用一组接口，靠 `model` 区分：

| `model` | 用途 | 上游关键参数 |
|---|---|---|
| `suno-v6` | 歌曲（有人声或纯音乐） | `mv` 取 `chirp-hawk` / `chirp-hawk-wild` / `chirp-goose` |
| `suno-sound` | 音效（可循环） | `mv` 取 `chirp-crow` / `chirp-fenix` |

**一次提交产出两首成品。** 上游每次生成返回两条 task，各自成歌（同一份歌词/描述的两个版本，
时长可能不同）。任务成功时 `result_urls` 里是**两条** mp3 地址，按 task 顺序排列。

成品在上游是 opus 编码的 `.m4a`，中台落库前统一转成 192kbps mp3 再传 OSS，所以拿到的
一定是可直接进剪辑软件的 mp3。

### 6.1 灵感模式：给一句话

`POST /v1/audio-generations/jobs`

```bash
curl -sS -X POST "$BASE_URL/v1/audio-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "suno-v6",
    "channel": "mxapi",
    "prompt": "一首关于长安的国风民谣，古筝与箫，苍凉但有力，男声",
    "title": "长安谣",
    "vocal_gender": "m",
    "external_ref": "music-order-001"
  }'
```

### 6.2 自定义模式：自己给歌词

把歌词写进 `lyrics`（用 `[Verse]`、`[Chorus]` 一类结构标签分段），**此时不要再给 `prompt`**，
两者互斥（同时给会返回 422）。

```bash
curl -sS -X POST "$BASE_URL/v1/audio-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "suno-v6",
    "lyrics": "[Verse]\n长安月，照我旧时衣\n[Chorus]\n一叶孤舟，万里向天涯",
    "tags": "cinematic chinese folk, guzheng, erhu, male tenor",
    "title": "长安谣",
    "style_weight": 0.7
  }'
```

纯音乐：给 `prompt`（风格描述）并置 `instrumental: true`，不要给 `lyrics`。

### 6.3 音效

```bash
curl -sS -X POST "$BASE_URL/v1/audio-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "suno-sound",
    "sound_model": "chirp-crow",
    "title": "Rain",
    "tags": "steady rain on a wooden roof, no thunder",
    "loop": true
  }'
```

参数：

| 字段 | 必填 | 可选值/限制 | 默认 |
|---|---:|---|---|
| `model` | 否 | `suno-v6`、`suno-sound` | `suno-v6` |
| `channel` | 否 | `mxapi`、`auto` | `mxapi` |
| `prompt` | 灵感模式必填 | 1～2000字符 | `""` |
| `lyrics` | 自定义模式必填 | 1～5000字符，带 `[Verse]` 等结构标签 | `""` |
| `tags` | 否 | 风格/声音描述，最多600字符 | `""` |
| `title` | 音效必填 | 最多100字符 | `""` |
| `instrumental` | 否 | `true` 为纯音乐 | `false` |
| `vocal_gender` | 否 | `m`、`f` | `null` |
| `style_weight` | 否 | 0～1，越高越贴 `tags` | `null` |
| `weirdness_constraint` | 否 | 0～1，越大越跳脱 | `null` |
| `music_model` | 否 | `chirp-hawk`、`chirp-hawk-wild`、`chirp-goose` | `chirp-hawk` |
| `sound_model` | 否 | `chirp-crow`、`chirp-fenix` | `chirp-crow` |
| `loop` | 否 | 音效是否做成可无缝循环 | `false` |
| `external_ref` | 否 | 最多256字符 | `null` |
| `metadata` | 否 | JSON对象 | `{}` |

### 6.4 查询结果

```bash
JOB_ID='替换为提交返回的job_id'

curl -sS -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/audio-generations/jobs/$JOB_ID"
```

成功后 `result_urls` 是两首 mp3；`upstream_response.songs` 里带着每首的
`title`、`duration_seconds`、`cover_url`（专辑图）、`model_name`、`tags` 与 `lyrics`
（上游自作词时回的最终歌词）。若一条 task 失败而另一条成歌，任务仍然算成功：
`result_urls` 只有成功那一首，失败原因记在 `upstream_response.task_errors` 里。

## 7. 语音合成与语音克隆

### 7.1 同步普通语音合成

`POST /v2/tts/speech`

```bash
curl -sS -X POST "$BASE_URL/v2/tts/speech" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "provider": "auto",
    "text": "您好，这是 AI Centre 2 的语音合成示例。",
    "language": "auto",
    "voice_profile_id": "default",
    "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0}
  }' \
  --output speech.wav
```

同步正文最多5000字符。更长文本请使用异步接口。

### 7.2 深度语音克隆

提供 `reference_audio_url` 即启用 VoxCPM2 深度克隆。`prompt_text` 最好填写参考音频的准确文本；为空时会先进行 ASR。

```bash
curl -sS -D tts-headers.txt -X POST "$BASE_URL/v2/tts/speech" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "text": "欢迎体验我们的新产品，它将为您带来更加自然的使用体验。",
    "language": "auto",
    "voice_profile_id": "default",
    "reference_audio_url": "https://storage.example.com/audio/reference.MP3",
    "prompt_text": "这是参考音频中准确说出的文字。",
    "emotion": "真诚、温暖、有感染力",
    "emotion_enhance": false,
    "clone_mode": "auto",
    "emotion_strategy": "auto",
    "quality_mode": "standard",
    "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0}
  }' \
  --output cloned.wav
```

主要可选值：

- `clone_mode`：`auto`、`controllable`、`ultimate`。
- `emotion_strategy`：`auto`、`inherit`、`force`。
- `quality_mode`：`standard`、`strict`；流式接口不支持 `strict`。
- `prosody.speed`：0.5～2.0；`volume`：0.1～2.0；`pitch`：0.5～2.0。

### 7.3 实时 PCM 流

`POST /v2/tts/speech/stream`

```bash
curl -sS -X POST "$BASE_URL/v2/tts/speech/stream" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "text": "这是实时流式语音合成示例。",
    "language": "auto",
    "voice_profile_id": "default"
  }' \
  --output speech.pcm

ffmpeg -f s16le -ar 48000 -ac 1 -i speech.pcm speech.wav
```

响应格式：48kHz、16-bit、单声道、little-endian PCM。请求ID位于 `X-TTS-Request-ID` 响应头。

### 7.4 异步长文本语音

`POST /v2/tts/jobs`，正文最多20000字符，适合8000字符等长内容。

```bash
curl -sS -X POST "$BASE_URL/v2/tts/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "text": "这里替换为长文本正文……",
    "language": "auto",
    "voice_profile_id": "default",
    "provider": "auto",
    "idempotency_key": "article-20260909-0001"
  }'
```

`idempotency_key` 必填，长度8～256字符；相同Key用于安全重试，不会重复创建任务。

```bash
JOB_ID='替换为提交返回的job_id'

curl -sS -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v2/tts/jobs/$JOB_ID"

curl -sS -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v2/tts/jobs/$JOB_ID/audio" \
  --output long-speech.wav
```

## 8. 视频与音频处理示例

### 8.1 唇形驱动与可选 GFPGAN

```bash
curl -sS -X POST "$BASE_URL/v1/lipsync/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "video_url": "https://storage.example.com/video/person.mp4",
    "audio_url": "https://storage.example.com/audio/speech.mp3",
    "face_restore": true
  }'
```

查询：`GET /v1/lipsync/jobs/{job_id}`。完成响应中的 `result_url` 为 OSS 地址。日志接口为 `GET /v1/lipsync/jobs/{job_id}/logs?stage=musetalk&tail=200`。

### 8.2 人脸处理

```bash
curl -sS -X POST "$BASE_URL/v1/face-mosaic/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "filename": "face_mosaic.mp4",
    "external_ref": "face-order-001",
    "metadata": {}
  }'
```

希望连接保持到OSS结果完成时，将路径改为 `/v1/face-mosaic/jobs/wait`。长视频建议使用异步路径。

### 8.3 SceneDetect 视频切片

```bash
curl -sS -X POST "$BASE_URL/v1/video-scenes/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "filename": "scene.mp4",
    "threshold": 27.0,
    "min_scene_len": 15,
    "external_ref": "scene-order-001"
  }'
```

- `threshold`：1～255；越小越敏感，切片通常越多。
- `min_scene_len`：最短场景帧数，不是秒数。
- 需要同步等待时使用 `/v1/video-scenes/jobs/wait`。

### 8.4 DA2/DA3 视频深度推理

```bash
curl -sS -X POST "$BASE_URL/v1/video-depth/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "version": "da2",
    "model": "small",
    "filename": "depth.mp4",
    "input_size": 518,
    "max_resolution": 960,
    "target_fps": -1,
    "external_ref": "depth-order-001"
  }'
```

- `version`：`da2` 或 `da3`。
- `model`：`small` 更快更省显存；`base` 细节更强。
- `input_size`：224～756且必须为14的倍数；518是推荐默认值。
- `max_resolution`：224～1920；不是固定值。
- `target_fps=-1` 保持原帧率，1～60表示目标抽帧率。

需要同步等待时使用 `/v1/video-depth/jobs/wait`。

### 8.5 视频超分

```bash
curl -sS -X POST "$BASE_URL/v1/video-upscale/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "provider": "auto",
    "max_resolution": 1920,
    "external_ref": "upscale-order-001",
    "metadata": {}
  }'
```

`provider` 可为 `auto`、`flashvsr`、`flashvsr_v2`、`seedvr2`；建议使用 `auto`。`max_resolution` 范围480～3840。

### 8.6 对白、音乐、音效和背景四轨分离

```bash
curl -sS -X POST "$BASE_URL/v1/audio-separation/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "model": "bandit-v2-multilingual",
    "filename_prefix": "separated",
    "external_ref": "audio-order-001",
    "metadata": {}
  }'
```

完成任务返回对白、音乐、音效和背景四个 OSS WAV 地址。需要同步等待时使用 `/v1/audio-separation/jobs/wait`。

### 8.7 授权素材水印处理

该接口仅用于调用方拥有处理权的素材和可见水印，不用于规避平台合法溯源标识。

```bash
curl -sS -X POST "$BASE_URL/v1/watermark-removal/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://storage.example.com/video/authorized-source.mp4",
    "filename": "watermark_removed.mp4",
    "mode": "light",
    "keep_intermediates": false,
    "external_ref": "watermark-order-001"
  }'
```

`mode` 可为 `light` 或 `intensive`。需要同步等待时使用 `/v1/watermark-removal/jobs/wait`。

## 9. 异步任务通用处理

### 9.1 状态

不同服务的字段名称可能略有差别，调用方应兼容以下常见状态：

| 状态 | 含义 |
|---|---|
| `queued` / `waiting_for_worker` | 已排队 |
| `running` / `processing` | 处理中 |
| `succeeded` / `completed` | 已成功 |
| `failed` | 失败，读取 `error` |
| `cancelled` | 已取消 |

轮询建议：前1分钟每3秒一次，之后每10秒一次；不要每秒高频查询。

### 9.2 通用 Shell 轮询示例

```bash
JOB_ID='替换为job_id'
STATUS_PATH='/v1/video-generations/jobs'

while true; do
  BODY=$(curl -sS -H "Authorization: Bearer $API_KEY" \
    "$BASE_URL$STATUS_PATH/$JOB_ID")
  echo "$BODY"
  STATE=$(printf '%s' "$BODY" | jq -r '.status // .state')
  case "$STATE" in
    succeeded|completed|failed|cancelled) break ;;
  esac
  sleep 5
done
```

### 9.3 幂等与业务关联

- TTS异步接口使用必填的 `idempotency_key`。
- 其他接口建议设置唯一 `external_ref`，但它不一定阻止重复提交。
- 网络超时后先查询已有任务或使用业务侧去重，再决定是否重试。

## 10. HTTP 状态码与排错

| HTTP状态码 | 含义 | 建议 |
|---:|---|---|
| 200 | 同步成功或查询成功 | 读取响应 |
| 202 | 异步任务已接受 | 保存 `job_id` 并轮询 |
| 400 | 请求逻辑错误 | 检查字段组合 |
| 401 | API Key缺失、格式错误、过期或无效 | 检查 `Bearer ` 前缀并联系管理员 |
| 404 | 路径或任务不存在 | 检查接口版本和 `job_id` |
| 409 | 状态冲突或结果尚未完成 | 稍后查询 |
| 413 | 文件过大 | 压缩素材或缩短视频 |
| 415 | 文件类型、扩展名或文件头不支持 | 转为受支持格式 |
| 422 | JSON字段或取值不符合约束 | 查看响应 `detail` |
| 429 | 请求过多 | 指数退避后重试 |
| 502/503 | 上游或Worker暂不可用 | 延迟重试并保留业务单号 |
| 504 | 超时 | 查询任务状态，不要立即重复提交 |

常见错误：

1. 把 Markdown 链接写法 `[https://...](https://...)` 原样放进 curl URL。curl 中只能填写裸 URL。
2. 请求头写成 `authorization: API_KEY`，遗漏 `Bearer `。
3. 把 Windows 本地路径直接传给服务器。
4. JSON 中字段名被转义成 `voice\_profile\_id`。
5. 用 `curl` 访问同步音频接口却没有 `--output`，导致二进制内容打印到终端。

## 11. 在线资源

- 本文档：`https://aicentre2.sligenai.cn:8443/api-docs.md`
- Swagger：`https://aicentre2.sligenai.cn:8443/docs`
- OpenAPI JSON：`https://aicentre2.sligenai.cn:8443/openapi.json`
- 管理后台中文说明：`https://aicentre2.sligenai.cn:8443/admin/external-api.html`

## 12. 安全说明

- API Key 泄露后应立即停用并重新签发。
- 不要在工单、群聊或公开文档中粘贴完整 API Key 和 OSS 签名 URL。
- 不要依赖返回错误文字做程序逻辑；优先使用 HTTP 状态码与任务状态字段。
- 返回的 OSS URL 可能有有效期，业务方应按自身合规策略及时转存。
- 不得使用接口处理无授权、侵权或违反适用法律法规的内容。
