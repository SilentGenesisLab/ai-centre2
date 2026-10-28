# AI Centre 2 对外 API 使用手册

版本：3.3  
更新日期：2026-09-30  
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
- 上传的素材同样受 §13 的保密要求约束：不要放进浏览器前端或公开仓库。

### 1.5 渠道选择：点名是优先，不是唯一

四个生成类接口（生图 §4、生视频 §5、生音乐 §6、定型决策 §9）都有 `channel` 字段，语义一致：

- **点名的渠道排在第一位，`auto`（或不传）等于不点名。** 后面自动跟上**同一型号**在绑定表里登记的其余渠道，按绑定优先级排列。
- 某一家**接不了这单**（未登记、被停用、健康状态不是 online，或型号/分辨率/时长/参考素材数量对不上）或**提交时报错**，就换下一家。所以点名一个不能用的渠道不再是硬错误：例如请求 `480p` 却传 `channel: "jmapi"`，中台会跳过它、改用能做 480p 的 `libtv`。
- **唯一不换渠道的情形：上游已经接单。** 这时的失败（提交之后才发生的超时、轮询途中的网络抖动、成品转存失败）不会再找下一家重投——上游那一单已经产生费用，重投就是再买一份，所以不会自动重复生成。也因此「作业失败」不等于「没花钱」：重投前先看任务里的 `channel`、`stage` 与 `error`，判断上游走到了哪一步。
- 换过渠道的作业从 `fallback_count` 读：`0` 表示用的就是第一顺位，`1` 表示换到了下一家，依此类推。任务里的 `channel` 是**实际出片的那一家**。

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
| 图片超分 | `POST /v1/image-upscale/jobs` | `POST /v1/image-upscale/jobs/wait` | `GET /v1/image-upscale/jobs/{job_id}` | `POST /v1/image-upscale/jobs/{job_id}/cancel` |
| 音频四轨分离 | `POST /v1/audio-separation/jobs` | `POST /v1/audio-separation/jobs/wait` | `GET /v1/audio-separation/jobs/{job_id}` | `POST /v1/audio-separation/jobs/{job_id}/cancel` |
| 授权视频水印处理 | `POST /v1/watermark-removal/jobs` | `POST /v1/watermark-removal/jobs/wait` | `GET /v1/watermark-removal/jobs/{job_id}` | `POST /v1/watermark-removal/jobs/{job_id}/cancel` |

### 2.7 定型决策

| 中文接口名 | 方法 | 路径 | 返回方式 |
|---|---|---|---|
| 创建定型决策任务 | POST | `/v1/decision-generations/jobs` | 异步任务 |
| 查询定型决策任务 | GET | `/v1/decision-generations/jobs/{job_id}` | JSON |
| 取消定型决策任务 | POST | `/v1/decision-generations/jobs/{job_id}/cancel` | JSON |

给一段事实描述和一组问题，拿回一组带置信度的定型答案（不是图片/视频/音频，所以 `result_urls` 恒为空）。详见第 9 节。

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

### 3.3 文本生成（火山方舟 / 阿里云百炼）

```bash
curl -sS -X POST "$BASE_URL/v1/text-generations/jobs"   -H "Authorization: Bearer $API_KEY"   -H 'Content-Type: application/json'   --data-raw '{
    "model": "deepseek-v4-flash",
    "prompt": "用一句话写一条关于秋天的产品标语",
    "system": "你是资深品牌文案",
    "temperature": 0.7,
    "max_tokens": 512,
    "explicit_label": false
  }'
```

| 字段 | 必填 | 说明 |
|---|---:|---|
| `model` | 否 | `deepseek-v4-flash`（默认）、`qwen3.8-omni-flash`、`qwen3.5-omni-plus` |
| `channel` | 否 | `auto`（默认）、`ark`、`dashscope` |
| `prompt` | 是 | 用户消息，1～20000 字 |
| `system` | 否 | 系统提示 |
| `temperature` | 否 | 0～2；传 `0` 是确定性输出（会原样传给上游，不会被当成"未设置"） |
| `max_tokens` | 否 | 1～8192，默认 1024 |
| `explicit_label` | 否 | 是否在正文末尾追加可见的「AI生成」尾注，默认 `false` |
| `output_audio` | 否 | 同时生成语音，默认 `false`。**只有 `qwen3.5-omni-plus` 支持**，别的型号传 `true` 会 422 |
| `voice` | 否 | 音色，默认 `Tina`，原样透传给上游 |

型号与渠道：

| `model` | 可用渠道 | 语音输出 |
|---|---|:--:|
| `deepseek-v4-flash` | `ark`（优先）、`dashscope` | — |
| `qwen3.8-omni-flash` | `dashscope` | **不支持** |
| `qwen3.5-omni-plus` | `dashscope` | 支持 |

结果在 `GET /v1/text-generations/jobs/{job_id}` 的 `upstream_response` 里：

| 字段 | 说明 |
|---|---|
| `text` | **打过标识的最终文本**（可见正文 + 可选尾注 + 不可见载荷），可直接交付 |
| `plain` | 可见正文（不含不可见标识），方便展示或做后续处理 |
| `aigc` | `{implicit, explicit, missing_fields}`，说明这次标了什么 |
| `usage` | 上游的 token 用量（要了语音时，`completion_tokens_details.audio_tokens` 是语音那部分） |
| `audio` | **只在要了语音时出现**：`{url, duration_seconds, voice, format, source_format}`；MP3 的 https 链接同时也在 `result_urls` 里 |

**隐式标识恒开**，与 `explicit_label` 无关：它用 Unicode 标签字符（U+E0000–U+E007F）
把 AIGC 载荷编码在正文末尾，肉眼不可见、能扛复制粘贴。用
`POST /v1/provenance/inspect` 传 `text=<那段文本>` 可以读回来。

> 边界：这种标识扛不住**主动清洗**（富文本编辑器、部分网关会剥掉标签字符）。
> 剥掉之后标识就没了 —— 这是文本标识的固有代价，不是实现问题。

#### 语音输出（Qwen Omni）的三个坑

1. **只有 `qwen3.5-omni-plus` 能出声。** `qwen3.8-omni-flash` 带上 `modalities:["text","audio"]`
   会被上游拒：`The current model does not support the modalities parameter containing audio`。
   平台在提交前就用 `422` 挡掉，不会先计费再报错。
2. **上游只认流式。** 非流式请求即使写了 `modalities:["text","audio"]`，响应体里的
   `audio` 也是 `null`，而 `usage` 里照样记 `audio_tokens` —— 也就是**按语音计费但拿不到语音**。
   平台这一支固定发 `stream:true`，调用方不用管。
3. **`max_tokens` 管不住语音长度。** 实测传 `max_tokens=64` 仍会生成 75 字正文 + 14.7 秒音频
   （`finish_reason=stop`）。要限制篇幅只能在 `prompt` / `system` 里写死。

产物形态：上游给的是**裸 PCM**（16 位、单声道、24kHz，没有 RIFF 头，即使请求里写的是
`format:"wav"`）。平台自己包 WAV 头再转 **MP3** 交付 —— 转 MP3 不只是为了小，也是因为
`provenance` 至今认不出 WAV，只有 MP3 打得进 AIGC 隐式标识。语音只做隐式标识
（音频上没有公认的可见加水印做法）。

```bash
# 要语音：加 output_audio
curl -sS -X POST "$BASE_URL/v1/text-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "qwen3.5-omni-plus",
    "channel": "dashscope",
    "prompt": "用两句话介绍杭州西湖",
    "output_audio": true,
    "voice": "Tina"
  }'

# 轮询到 succeeded 后，upstream_response.audio.url 就是 MP3 的公网链接
curl -sS "$BASE_URL/v1/text-generations/jobs/<job_id>" -H "Authorization: Bearer $API_KEY"
```


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
| `model` | 否 | `gpt-image-2`、`gpt-image-2.5`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、`nano-banana-2`、`nanobanana-2.1`、`midjourney`、`moss-image` | `gpt-image-2` |
| `channel` | 否 | `grsai`、`teamorouter`、`wuyinkeji`、`huanwangai`、`moss-local`、`auto` | `grsai` |
| `prompt` | 是 | 1～10000字符 | — |
| `reference_image_urls` | 否 | 最多9张公网 HTTPS 图片 | `[]` |
| `aspect_ratio` | 否 | `1:1`、`2:3`、`3:2`、`3:4`、`4:3`、`9:16`、`16:9` | `1:1` |
| `image_size` | 否 | `1K`、`2K`、`4K` | `1K` |
| `explicit_label` | 否 | 是否在左上角叠加「AI生成」角标（见第 10 节） | `false` |
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

### 4.3 TeamORouter 渠道

`channel` 传 `teamorouter` 走第三方 TeamORouter 的生图接口，目前接了三个型号：

| `model` | 上游型号 | 参考图 | 说明 |
|---|---|---|---|
| `gpt-image-2` | `gpt-image-2` | 不支持 | |
| `gpt-image-2.5-sunburst` | `gpt-image-2.5-sunburst` | 不支持 | 与 `grsai` 的同名型号是不同上游，出图风格不同 |
| `gpt-image-2.5-flare` | `gpt-image-2.5-flare` | 不支持 | 同上 |

- **只吃纯文本**：`/v1/images/generations` 不接收参考图（编辑类接口的契约未实测，未开放）。带 `reference_image_urls` 的请求会被本地拦下：`teamorouter` 不支持参考图，这类请求会落到 `grsai`，点名 `teamorouter` 也一样（见 §1.5）。
- **同步返回**：上游一次调用直接给成品，没有任务号。所以作业的 `upstream_task_id` 恒为空、`result_urls` 由中台把上游返回的内联图片转存到对象存储后给出，其余字段与 `grsai` 一致。
- **尺寸由中台按 `aspect_ratio` + `image_size` 计算**：上游不校验尺寸，传错不会报错、只会静默给一张默认尺寸的图，所以不要指望上游纠正。
- 候选顺序是 `grsai` → `teamorouter`（后者登记为兜底，优先级更低）。`channel` 传 `teamorouter` 只是把它挪到排头，`grsai` 仍跟在后面。

### 4.4 速创渠道（wuyinkeji）

`channel` 传 `wuyinkeji` 走速创（`api.wuyinkeji.com`）的异步生图接口。目前一个型号：

| `model` | 上游型号 | 参考图 | 说明 |
|---|---|---|---|
| `nanobanana-2.1` | `NanoBanana2.1` | 最多 9 张 | 与 `grsai` 的 `nano-banana-2` 是不同上游、不同代数 |

- **异步 + 轮询**：提交拿到 `image_<uuid>` 形式的任务号，中台按 `GET /api/async/detail?id=` 轮询，成功后把上游返回的图片转存到对象存储再给出 `result_urls`。与 `teamorouter` 的同步返回不同。
- **参考图走 `urls` 字段**，上游收的是**逗号分隔的一个字符串**，不是数组 —— 传数组会回 500「转发请求失败」。中台已按串拼好，调用方无感知。
- **认证是裸 `Authorization: <key>`**，不带 `Bearer ` 前缀。这不是随意选择：上游的**查询**端点带前缀会直接回 403，改用 `X-API-Key` 也不认（提交端点反而三种都收）。所以这个渠道的 `auth_type` 是单独的一种。
- **状态是数字**：`0` 排队、`2` 成功（实测）；中台会翻译成统一的 `queued`/`succeeded`，调用方只看 `status` 字段即可。
- **上游不校验 `image_size`**：传 `8K` 这类非法值照样受理并回一张图，不会报错。所以尺寸错了是静默的，别指望上游纠正。
- **HTTP 恒为 200，错误在响应体的 `code` 字段里**（如 `403` 密钥错误、`400` 错误的ID）。中台已按响应体判定，渠道探针也据此区分「密钥有效」与「密钥不认」。

### 4.5 幻网AI 渠道（huanwangai，Midjourney）

`channel` 传 `huanwangai`、`model` 传 `midjourney`，走幻网 AI（`api.huanwangai.com`）的 Midjourney 接口。目前一个型号：

| `model` | 上游 `botType` | 参考图 | 说明 |
|---|---|---|---|
| `midjourney` | `MID_JOURNEY` | **不支持**（见下） | 一次 imagine 出 **4 张独立图** |

- **画幅要写进 prompt**。Midjourney 没有独立的画幅字段，`--ar` 是 prompt 字符串的一部分。中台会把请求里的 `aspect_ratio` 拼成 `--ar 16:9` 追加到 prompt 末尾；**prompt 里已经写过 `--ar` 就不再加**（MJ 对重复的 `--ar` 取最后一个，硬拼会盖掉调用方的意图）。其余 MJ 参数（`--v`、`--style`、`--sref`、`--stylize`…）调用方直接写在 prompt 里即可。
- **一次生成返回 4 张图**，`result_urls` 就是这 4 张独立图的地址（不是那张 2×2 拼图）。升级/单张动作只回一张时，才回落到拼图。
- **不支持参考图**。MJ 的「垫图/风格参考」要 base64 图片（`base64Array`），而中台这层拿到的是公网 URL，中间缺一次下载+编码，所以这一版把 `midjourney` 的输入声明成**纯文本** —— 带参考图的请求不会选到它。想用风格参考可以在 prompt 里写 `--sref <公网URL>`，MJ 自己去取。
- **认证是裸 `Authorization: <key>`**（`sk-mj-...`，不带 `Bearer ` 前缀），与 `wuyinkeji` 同一种 `auth_type`。
- **HTTP 恒为 200，成败在响应体里**：失败是 `{"success":false,"code":-1,"message":"prompt不能为空"}`。中台按 `success` 判定，报错信息取自 `message`。
- **状态是英文词**：`SUCCESS` / `FAILURE` / `IN_PROGRESS`，中台的状态表直接认（`FAILURE` 因此补进了 `FAILURE_STATES`）。
- **失败原因在 `failReason`**（camelCase），中台已把它接进错误详情。
- **探针是强判据**：鉴权发生在查库之前，所以探一个不存在的任务号时，`401` = key 不认、`404` = 网关通且 key 有效、`200` = 通。这一条同时证明了「网关通」与「密钥有效」，与 `teamorouter` 同级。

### 4.6 本地渠道（moss-local，MOSS-Image）

`channel` 传 `moss-local`、`model` 传 `moss-image`，走**本机 GPU 上自部署**的
MOSS-Image-SFT（OpenMOSS，Apache-2.0）。它不消耗第三方额度，也不出公网。

| `model` | 上游型号 | 参考图 | 尺寸 |
|---|---|---|---|
| `moss-image` | `moss-image` | **不支持**（纯文生图） | 只支持 `1K`、`2K` |

- **它是独立型号，只能点名调用**。`auto` 不会把它当成别家模型的兜底 —— 换个模型等于换一张
  画风不同的图，静默换掉比失败更难查。注意 `channel` 的默认值是 `grsai`，所以用这个型号时
  **必须显式传 `channel`**（`moss-local` 或 `auto`）；不传会 `404 model or channel not found`
  —— 与 `midjourney` 用的是同一套约定。
- **强在文字与排版**：中文多行文字、数字（订单号/金额/日期）、以及 JSON layout 按 bbox 定位
  排版都是它的长处；写实人物与场景也够用。弱在**被曲面/小尺寸挤压的小字**（例如表盘铭文）会糊。
- **尺寸上限**：`image_size` 只接受 `1K`/`2K`。`4K` 是 16.6 Mpx，本机显存跑不动，请求会直接
  `422`（没有兼容渠道），不会先收下再失败。同一个模型换别家跑 4K 是允许的。
- **速度**：1K 约 **37 秒/张**，2K 约 **200 秒/张**（1024² 峰值显存约 11.6 GB）。
  服务空闲 30 分钟会把模型挪回内存，下一次请求首图多花约 3 秒。
- **可复现**：同一 prompt + 宽高在同一个版本上输出逐字节相同（内部固定 seed）。
- **同步返回**：与 `teamorouter` 一样，提交时不产生任务号，中台在提交返回处就收下成品、
  打 AI 标识、转存对象存储，再给 `result_urls`。

## 5. 生视频接口

### 5.1 通用视频生成

`POST /v1/video-generations/jobs`

可同时传多张图片、多个视频和多段音频。具体支持数量取决于渠道与模型：`seedance-2.0` 是 `jmapi` 最多9图、3视频、3音频，`libtv` 最多9图、3视频且不接收独立音频；`seedance-2.5` 是两个渠道都最多30图、10视频（参考视频/音频总时长上限 30 秒），独立音频只有 `jmapi` 接收；`minimax-h3-rh-enhanced` 走 `runninghub`，最多9图、3视频、3音频（见 5.1.2）。

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
| `model` | 否 | `seedance-2.0`、`seedance-2.5`、`minimax-h3-rh-enhanced` | `seedance-2.0` |
| `channel` | 否 | `jmapi`、`libtv`、`runninghub`、`auto` | `jmapi` |
| `prompt` | 是 | 1～10000字符 | — |
| `reference_image_urls` | 否 | 2.0最多9张；2.5最多30张；H3最多9张 | `[]` |
| `reference_video_urls` | 否 | 2.0最多3个；2.5最多10个；H3最多3个 | `[]` |
| `reference_audio_urls` | 否 | jmapi最多3个（2.5最多10个）；libtv不支持；H3最多3个 | `[]` |
| `duration_seconds` | 否 | 2～30秒；`seedance-2.0` 上限15秒，H3 是 4～15秒 | 5 |
| `resolution` | 否 | `480p`、`720p`、`768p`、`1080p`、`2K`；`768p` 只有 H3 接受 | `720p` |
| `aspect_ratio` | 否 | `9:16`、`16:9`、`1:1`、`4:3`、`3:4` | `9:16` |
| `sound` | 否 | `true`保留上游音轨；`false`在OSS转存前确定性移除音轨 | `false` |
| `external_ref` | 否 | 最多256字符 | `null` |
| `metadata` | 否 | JSON对象 | `{}` |

`channel` 省略时默认 `jmapi`，但点名只是「先用它」：候选列表是点名的那家排头、同型号其余渠道按绑定优先级跟在后面（`seedance-2.0`/`seedance-2.5` 是 `jmapi` + `libtv`，`minimax-h3-rh-enhanced` 只有 `runninghub` 一条）。接不了这单或提交报错就换下一家，换过几家看任务的 `fallback_count`；上游一旦接单就不再换，详见 §1.5。

渠道分辨率说明：当前 jmapi 的 `seedance2.0_vip` 不接受 `480p`，所以请求 480P 时这家会被跳过、由 libtv 接单——传 `channel: "jmapi"` 也一样，不会返回 422；720P 已在两个渠道完成生产验证。

### 5.1.1 Seedance 2.5

`model` 传 `seedance-2.5` 即可，两个渠道都提供：

| 渠道 | 上游模型 | 时长 | 分辨率 | 参考素材 |
|---|---|---|---|---|
| `jmapi` | `model_version=seedance2.5` | 4～30秒 | `480p`、`720p` | 最多30图、10视频、10音频；视频/音频**合计**时长 ≤30秒 |
| `libtv` | `star-video2.5` | 4～30秒 | `480p`、`720p`、`1080p` | 最多30图、10视频；不接收独立音频 |

- `480p` 只有 2.5 能做：jmapi 侧 2.0 会被上游拒绝，2.5 可以。
- `1080p` 在 jmapi 侧不可用（上游只认 480p/720p），所以会落到 libtv——点名 `jmapi` 时同样如此，不会返回 422。
- 时长超过 15 秒的请求只有 2.5 能接，显式指定 `seedance-2.0` 时同样在提交前返回422，不会把必然被上游拒绝的请求发出去。
- 上游还支持 `21:9` 和 `adaptive` 画幅，当前接口的 `aspect_ratio` 尚未开放这两个值。
- 结果与 2.0 一样先转存到 AI Centre 的 OSS 再返回 `result_urls`。

### 5.1.2 MiniMax H3（RunningHub 增强版）

`model` 传 `minimax-h3-rh-enhanced`，走 `runninghub` 渠道：

| 渠道 | 上游模型 | 时长 | 分辨率 | 参考素材 |
|---|---|---|---|---|
| `runninghub` | `minimax-h3-rh-enhanced` | 4～15秒（整数） | `480p`、`768p`、`1080p` | 最多9图、3视频、3音频 |

```bash
curl -sS -X POST "$BASE_URL/v1/video-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "minimax-h3-rh-enhanced",
    "channel": "runninghub",
    "prompt": "故事主题:甜酷女团主题，节奏明快，自信飒爽，舞台感强……",
    "reference_image_urls": ["https://storage.example.com/images/ref1.png"],
    "duration_seconds": 10,
    "resolution": "768p",
    "aspect_ratio": "9:16",
    "sound": false
  }'
```

- **`768p` 只有这个模型接受**，它正是 H3 的原生档；`720p` 反过来只有 Seedance 接。两者配错组合会在提交前返回422，不会把必然被上游拒的请求发出去。
- 时长是 **4～15 秒的整数**，2～3 秒会在提交前返回422。
- 画幅只开放 `9:16`、`16:9`、`1:1`、`4:3`、`3:4` 五个值；上游白名单比这更宽（另有 `2:3`、`3:2`、`21:9`），当前接口未开放。
- 上游的 `audioMode` 固定用 `native`（模型自己生成音轨）。上游另有 `lock_source`、`remix_source`、`reference_only` 三档，语义没有实测过，因此没有开放。`sound: false` 仍然被尊重：它由转存那一步确定性摘掉音轨（和 Seedance 同一条路径），不是靠上游静音。
- 结果同样先转存到 AI Centre 的 OSS 再返回 `result_urls`。
- 渠道密钥走部署级环境变量 `RUNNINGHUB_API_TOKEN`（与视频超分共用一份），不落能力库；需要覆盖时在管理端给该渠道单独填一次凭据。
- **上游的查询接口只校验 `Authorization` 头在不在，不校验它对不对**（2026-09-29 实测：错的 token 与对的 token 返回逐字相同）。所以「连接测试通过」只说明网关通、头带了，不代表密钥有效；密钥错了会在真正提交生成时才暴露。

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

候选渠道不兼容（提交前判定）、提交被上游拒绝、以及上游明确进入失败终态，都会尝试下一可用渠道；超时或结果转存失败不会自动重复生成，以免产生两个远端成片和重复费用。这条规则对点名渠道与 `auto` 一视同仁，详见 §1.5。

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

渠道：`mxapi`（Suno）。两种能力共用一组接口，靠 `model` 区分（选路规则与 §1.5 一致；目前两个型号都只绑了这一条，暂时无处可换）：

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

`provider` 可为 `auto`、`flashvsr`、`flashvsr_v2`、`seedvr2`、`starlight26`、`starlightfast3`；
建议使用 `auto`。`max_resolution` 范围480～3840。

**Starlight（Yotta / Topaz 后端）的四个约定：**

| `provider` | 上游模型 | 特点 |
|---|---|---|
| `starlight26` | `slp-2.6`（Starlight Precise 2.6） | 画质优先 |
| `starlightfast3` | `slf-3`（Starlight Fast 3） | 更快，官方称 4× 速度、4K 观感相当 |

1. **只在点名时可用，默认不进 `auto`。** 它按帧计费，塞进 `auto` 等于给存量调用换价目表。
   要让它参与自动路由，把 `starlight26` 写进 `VIDEO_UPSCALE_AUTO_PROVIDER_ORDER`。
2. **按帧计费**（实测约 0.0042/帧，与时长无关），`failed` 的任务也可能已在途。
   提交成功后只能轮询同一个 `request_id`，**平台内部已保证不重投**。
3. **并发上限按账号算**（每账号 2 个在途），平台把 `YOTTA_UPSCALE_API_KEYS` 里的多个
   key 做成轮转池，加账号就线性提速；全满时排队而不是报错。
4. **产物无音轨** —— 与其余 provider 一致：平台装配时丢掉上游音轨、挂回源片音轨，
   所以成片的声音不受影响。

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

### 8.8 图片超分

中台自己跑的 **SeedVR2**（不是转发第三方），跑在服务器 GPU0 上，所以**不吃上游排队**，但一张要十几到几十秒。

与视频超分最大的不同是**一次可以提交多张**：参考图天然是成批的，而且模型常驻在 worker 进程里，一批只加载一次。

```bash
curl -sS -X POST "$BASE_URL/v1/image-upscale/jobs"   -H "Authorization: Bearer $API_KEY"   -H 'Content-Type: application/json'   --data-raw '{
    "source_uris": [
      "https://storage.example.com/ref/a.png",
      "https://storage.example.com/ref/b.jpg"
    ],
    "model": "auto",
    "target_short_side": 1080,
    "external_ref": "ref-batch-001",
    "metadata": {}
  }'
```

| 字段 | 必填 | 说明 |
|---|---:|---|
| `source_uris` | 是 | 1～32 张公网 HTTPS 图片（上限由 `IMAGE_UPSCALE_MAX_IMAGES` 决定） |
| `model` | 否 | `auto`（默认，跟随服务端配置）／`3b`（fp8，推荐）／`7b-sharp` |
| `target_short_side` | 否 | 目标**短边**像素，256～4096，默认 1080 |

**`target_short_side` 是目标分辨率，不是放大倍数。** SeedVR2 做的是"按目标分辨率修复式放大"，所以没有 2×/4× 这种概念；给 1080 就是出短边 1080 的图。

**返回结构与别的接口不同：逐张给结果。**

```json
{
  "job_id": "…",
  "status": "succeeded",
  "model": "3b",
  "target_short_side": 1080,
  "image_count": 2,
  "succeeded_count": 1,
  "failed_count": 1,
  "result_urls": ["https://oss…/000.png"],
  "results": [
    {"source_uri": "…/a.png", "status": "succeeded", "result_url": "https://oss…/000.png",
     "source_size": "512x512", "output_size": "1080x1080", "seconds": 18.4},
    {"source_uri": "…/b.jpg", "status": "failed", "error": "RuntimeError: …"}
  ],
  "elapsed_seconds": 41.2
}
```

**单张失败不改作业状态**：`status` 仍是 `succeeded`，坏图记在 `results` 里并计入 `failed_count`。批量参考图里，一张坏图让整批返回「失败」会让调用方分不清「全挂了」和「挂了一张」——判断成败要读 `succeeded_count`／`failed_count`，不要只看 `status`。

## 9. 定型决策（Jev）

`POST /v1/decision-generations/jobs`

给一段事实描述（`state`）和一组问题（`questions`），一次调用拿回一组带置信度的定型答案。它不产出图片/视频/音频，所以 `result_urls` 恒为空，答案在 `upstream_response.answers` 里。

```bash
curl -sS -X POST "$BASE_URL/v1/decision-generations/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "model": "jev",
    "channel": "teamorouter",
    "state": "客户 09-30 提出改枪皮配色，此前已返工两轮，交付期还剩 5 天。",
    "questions": {
      "risk": {
        "type": "choice",
        "instructions": "这一轮返工的风险有多高？",
        "criteria": {"low": "只是微调", "medium": "需要重渲", "high": "方向要重定"}
      },
      "urgency": {"type": "noul", "instructions": "这件事的紧迫程度。"}
    },
    "external_ref": "decision-001"
  }'
```

参数：

| 字段 | 必填 | 可选值/限制 | 默认 |
|---|---:|---|---|
| `model` | 否 | `jev` | `jev` |
| `channel` | 否 | `teamorouter`、`auto` | `teamorouter` |
| `state` | 是 | 1～20000字符的事实描述 | — |
| `questions` | 是 | 非空对象，每个问题含 `type` 与 `instructions` | — |
| `external_ref` | 否 | 最多256字符 | `null` |
| `metadata` | 否 | JSON对象 | `{}` |

三种题型（`criteria` 的形状**互不相同**，喂错上游会拒绝并在错误信息里说明）：

| `type` | `criteria` | 返回 |
|---|---|---|
| `choice` | 对象：key → 选项说明，2～255 项 | `choice`（选中的 key）、`confidence`、`probabilities` |
| `score` | 数组：级别名，2～10 级 | `score`、`confidence`、`legend`、`probabilities` |
| `noul` | 不给 | `noul`（0～1 的数值） |

`questions` 在本地只校验「非空、每个问题有 `type` 和 `instructions`」，题型结构原样透传给上游——上游的校验比我们能表达的细，本地复刻只会把合法请求挡在门外。

提交成功返回 HTTP 202（`status_url` 与生图接口同一形状）。查询：

```bash
JOB_ID='替换为提交返回的job_id'

curl -sS \
  -H "Authorization: Bearer $API_KEY" \
  "$BASE_URL/v1/decision-generations/jobs/$JOB_ID"
```

完成时 `status` 为 `succeeded`，答案形如：

```json
{
  "status": "succeeded",
  "upstream_task_id": "",
  "result_urls": [],
  "upstream_response": {
    "model": "typesafe-ai/jev",
    "answers": {
      "risk": {"type": "choice", "choice": "medium", "confidence": 0.72,
               "probabilities": {"low": 0.2, "medium": 0.72, "high": 0.08}},
      "urgency": {"type": "noul", "noul": 0.4}
    },
    "usage": {"prompt_tokens": 118, "completion_tokens": 42}
  }
}
```

它是同步短调用（不轮询、不转存、不占 GPU），但**作业接口的形状与生图一致**：先拿 `job_id`、再查询状态。答案一旦拿到就会写进记录，即使随后调用方撤单也不会丢。

选路规则与 §1.5 一致（目前只有 `teamorouter` 一条绑定，暂时无处可换）。有一处不同要留意：**答案已经拿到之后的失败不会再换渠道重问**——上游每次调用都重新计费，而定型判定是概率输出，第二次未必给同一份答案，手上这份比换一家更值钱。

## 10. AI 生成内容标识（算法备案）

> 对接方只需要看这一节；更完整的说明（字段含义、覆盖范围、已知边界、常见问题）见
> [AIGC_LABELING.md](AIGC_LABELING.md)。

生成类产物（**图像生成 / 通用视频生成 / MiniMax H3 / 音乐与音效**）会带上两类标识。

### 10.1 隐式标识（溯源，恒开）

写进**文件容器本身的元数据**，不改变画面与听感，可以用第 10.3 节的接口读回来核验。

| 容器 | 写在哪 |
|---|---|
| PNG | `iTXt` 块（UTF-8，关键字 `AIGC`） |
| JPEG | `COM` 段 |
| MP3 | ID3v2 的 `TXXX` 帧 |
| MP4 / MOV | `comment` 元数据 |

内容是这段 JSON：

```json
{ "AIGC": {
  "Label": "1",
  "ContentProducer": "生成合成服务提供者的名称或编码",
  "ProduceID": "算法服务版本号 + 内容 ID 的 MD5",
  "ReservedCode1": "文件名 + 生成时间的 MD5",
  "PropagatorID": "时间戳 + 调用方标识的 MD5（见下）",
  "ContentPropagator": "企业社会信用代码",
  "ReservedCode2": "hash:<信用代码的 MD5>"
}}
```

字段由服务端配置（`AIGC_CONTENT_PRODUCER` / `AIGC_CONTENT_PROPAGATOR` / `AIGC_ALGORITHM_VERSION`）：

| 配置项 | 取值 |
|---|---|
| `AIGC_CONTENT_PRODUCER` | 企业统一社会信用代码（**只由环境变量给**） |
| `AIGC_CONTENT_PROPAGATOR` | 企业统一社会信用代码（与上一项同值，**只由环境变量给**） |
| `AIGC_ALGORITHM_VERSION` | `sligenai-aigc-v1` |

> 统一社会信用代码是政府发的 18 位法定标识，**必须与备案主体一致**，且**只从环境变量
> 注入**（不进公开仓库）。没配置时这两个字段为空、`ReservedCode2` 也为空，
> 作业结果里会标注「企业信息未配置」。改动等于变更备案主体，`ReservedCode2` 会跟着变。

**两点如实说明，别当它是完整实现：**

1. **`PropagatorID` 是替代项。** 规范要的是「时间戳 + 用户ID + 手机号」，而中台不知道调用方的用户是谁，手上只有 `external_ref`。当前用的是「时间戳 + external_ref」。等有了用户体系再换。
2. **企业信息没配置时**，仍会写入 `Label` 与本地可推导的字段，但**不会静默假装完整** —— 作业结果里会带上 `企业信息未配置` 的说明。

### 10.2 显式标识（画面角标，默认关）

请求里传 **`explicit_label: true`** 才会在**左上角**叠加「AI生成」角标。

```bash
curl -sS -X POST "$BASE_URL/v1/image-generations/jobs"   -H "Authorization: Bearer $API_KEY"   -H 'Content-Type: application/json'   --data-raw '{"model":"nano-banana-2","prompt":"一只猫","explicit_label":true}'
```

- **默认关**：存量调用方不会突然多一个角标。
- **只对图片与视频有效**。音频忽略这个字段 —— 音频上没有公认的可见标识做法，强行插水印音只会牺牲听感；音频的标识一律走隐式。
- 角标宽度取画面宽的 12%（上限 22%，避免小图上喧宾夺主），位置固定左上角。

### 10.3 读回标识

两种输入**二选一**：本地上传，或公网 HTTPS 直链（服务端去取）。

```bash
# 本地上传
curl -sS -X POST "$BASE_URL/v1/provenance/inspect"   -H "Authorization: Bearer $API_KEY"   -F "file=@downloaded.png"

# 公网直链（比如中台 OSS 上的产物）
curl -sS -X POST "$BASE_URL/v1/provenance/inspect"   -H "Authorization: Bearer $API_KEY"   -F "url=https://oss.example.com/result.png"
```

两个都给、或都不给，都回 422。URL 走公开 IP 校验（防 SSRF），内网地址不会去取。

返回：

```json
{
  "filename": "downloaded.png",
  "source": "upload",
  "size": 123456,
  "sha256": "…",
  "found": true,
  "container": "png",
  "where": "PNG iTXt",
  "payload": { "AIGC": { … } },
  "parseable": true,
  "raw": "{\"AIGC\":{…}}"
}
```

- **`found: false` 是正常答案**，不是错误 —— 这个接口要能对着任意文件回答「有没有标识」。
- `parseable: false` 表示读到了内容但不是我们的 JSON（可能是别的工具写的 `comment`），原文在 `raw` 里。
- 该接口**只读、不落对象存储**：它是核验用的口子，不顺手把文件传到 OSS 上。

### 10.4 已知边界

- **标识依赖中台在本地留一份产物**（生成结果会先落到内核存储再给出 `result_urls`）。若部署没配 `KERNEL_UPLOAD_URL`，生成链路会直接透传上游链接，那条路上**没有本地文件可标**。
- **下游二次转码可能抹掉元数据**。平台自己压缩、转封装、截图都可能丢掉容器的 `comment`/`iTXt`。要长期溯源，得连原始文件一起留档。
- 处理类能力（视频超分、调色、唇形驱动、人脸处理、切片、水印处理等）**不加标识**。

## 11. 异步任务通用处理

### 10.1 状态

不同服务的字段名称可能略有差别，调用方应兼容以下常见状态：

| 状态 | 含义 |
|---|---|
| `queued` / `waiting_for_worker` | 已排队 |
| `running` / `processing` | 处理中 |
| `succeeded` / `completed` | 已成功 |
| `failed` | 失败，读取 `error` |
| `cancelled` | 已取消 |

轮询建议：前1分钟每3秒一次，之后每10秒一次；不要每秒高频查询。

### 10.2 通用 Shell 轮询示例

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

### 10.3 幂等与业务关联

- TTS异步接口使用必填的 `idempotency_key`。
- 其他接口建议设置唯一 `external_ref`，但它不一定阻止重复提交。
- 网络超时后先查询已有任务或使用业务侧去重，再决定是否重试。

## 12. HTTP 状态码与排错

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

## 13. 在线资源

- 本文档：`https://aicentre2.sligenai.cn:8443/api-docs.md`
- Swagger：`https://aicentre2.sligenai.cn:8443/docs`
- OpenAPI JSON：`https://aicentre2.sligenai.cn:8443/openapi.json`
- 管理后台中文说明：`https://aicentre2.sligenai.cn:8443/admin/external-api.html`

## 14. 安全说明

- API Key 泄露后应立即停用并重新签发。
- 不要在工单、群聊或公开文档中粘贴完整 API Key 和 OSS 签名 URL。
- 不要依赖返回错误文字做程序逻辑；优先使用 HTTP 状态码与任务状态字段。
- 返回的 OSS URL 可能有有效期，业务方应按自身合规策略及时转存。
- 不得使用接口处理无授权、侵权或违反适用法律法规的内容。
