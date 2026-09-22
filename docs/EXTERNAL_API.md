# AI Centre 2 第三方接口文档

版本：2.5  
更新日期：2026-08-20  
生产地址：`https://aicentre2.sligenai.cn:8443`

- Swagger：`https://aicentre2.sligenai.cn:8443/docs`
- OpenAPI JSON：`https://aicentre2.sligenai.cn:8443/openapi.json`
- 在线中文文档：`https://aicentre2.sligenai.cn:8443/admin/external-api.html`

## 一、接入前必须知道

除“检查系统健康状态”外，所有请求必须携带：

```http
Authorization: Bearer <SERVICE_TOKEN>
Content-Type: application/json
```

`<SERVICE_TOKEN>` 是占位符，不是可以直接发送的文字。实际 Token 由 AI Centre 管理员单独分配，交付第三方时应通过私聊或密码管理器发送，不能嵌入公开文档。

鉴权值必须包含 `Bearer ` 前缀，且前缀与 Token 之间有一个空格：

```text
正确：Authorization: Bearer 实际SERVICE_TOKEN
错误：Authorization: 实际SERVICE_TOKEN
```

Shell 中建议先设置变量，避免在每条命令中重复 Token：

```bash
export SERVICE_TOKEN='粘贴管理员单独发给你的Token'
curl -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  "https://aicentre2.sligenai.cn:8443/v2/tts/providers"
```

`SERVICE_TOKEN` 只能保存在调用方服务端，不得写入网页、App、Git、日志或截图。

公开接口只接收公网 HTTPS 素材 URL，不接收 `C:\...`、`K:\...` 等本地路径，也不接收 multipart 文件。URL 可以携带对象存储签名参数，但不得指向本机、内网、云元数据地址或包含用户名密码。视频和音频单文件最大 512 MiB；OCR 单图最大 20 MiB、单批最多 20 张。

### 本地素材如何调用

示例原文件：

```text
K:\三瑞集团：尊爱AI视频需求汇总\视频脚本\脚本1\女1.mp4
K:\三瑞集团：尊爱AI视频需求汇总\视频脚本\脚本1\女1.MP3
```

先把音频裁成前 10 秒，原文件不会被覆盖：

```powershell
ffmpeg -ss 0 -i "K:\三瑞集团：尊爱AI视频需求汇总\视频脚本\脚本1\女1.MP3" `
  -t 10 -map 0:a:0 -c:a libmp3lame -q:a 2 `
  "K:\三瑞集团：尊爱AI视频需求汇总\视频脚本\脚本1\女1_前10秒.mp3"
```

然后把视频和 `女1_前10秒.mp3` 上传到调用方自己的对象存储，获得两个公网 HTTPS URL。URL 只需在提交下载期间有效；接口返回成功后，中台已持久化素材，不再依赖原 URL。

## 二、接口分类与中文名称

| 分类 | 中文接口名 | 方法与路径 |
|---|---|---|
| 系统状态 | 检查中台与上游服务健康状态 | `GET /health` |
| 唇形驱动 | 通过视频和音频 URL 创建唇形驱动任务 | `POST /v1/lipsync/jobs` |
| 唇形驱动 | 查询任务列表、状态、日志、下载结果、取消任务 | `/v1/lipsync/jobs...` |
| 语音识别 | 通过音视频 URL 识别文字 | `POST /v1/asr/transcriptions` |
| 语音合成 | 语音合成或 VoxCPM2 深度语音克隆 | `POST /v2/tts/speech` |
| 语音合成 | 实时 PCM 流式语音合成 | `POST /v2/tts/speech/stream` |
| 语音合成 | 查询异步质量审计 | `GET /v2/tts/quality/{request_id}` |
| 语音合成 | 创建、查询和下载异步语音合成任务 | `/v2/tts/jobs...` |
| OCR 文字识别 | 通过图片 URL 批量识别文字 | `POST /v1/ocr/batch` |
| 人脸处理 | 通过视频 URL 创建人脸处理任务 | `POST /v1/face-mosaic/jobs` |
| 人脸处理 | 提交人脸处理并等待 OSS 结果 | `POST /v1/face-mosaic/jobs/wait` |
| 视频切片 | 创建异步 SceneDetect 视频切片任务 | `POST /v1/video-scenes/jobs` |
| 视频切片 | 高优先级提交并等待 OSS 切片结果 | `POST /v1/video-scenes/jobs/wait` |
| 水印处理 | 创建异步视频水印处理任务 | `POST /v1/watermark-removal/jobs` |
| 水印处理 | 提交并同步等待处理结果 | `POST /v1/watermark-removal/jobs/wait` |
| 视频深度推理 | 创建异步 DA2/DA3 Small/Base 任务 | `POST /v1/video-depth/jobs` |
| 视频深度推理 | 高优先级提交并等待 OSS 深度视频 | `POST /v1/video-depth/jobs/wait` |
| 音频分离 | 创建异步对白、音乐、音效分离任务 | `POST /v1/audio-separation/jobs` |
| 音频分离 | 高优先级提交并等待四轨 OSS 结果 | `POST /v1/audio-separation/jobs/wait` |
| 视频超分 | 智能渠道异步超分，失败自动切换 | `POST /v1/video-upscale/jobs` |
| 视频超分 | 高优先级提交并等待超分结果 | `POST /v1/video-upscale/jobs/wait` |
| MiniMax H3视频生成 | 创建单段或两段异步参考视频生成任务 | `POST /v1/video-generations/minimax-h3/jobs` |
| AI 视频拉片 | 无参考拆镜、画面、声音与问题定位 | `POST /v1/video-reviews/jobs` |
| AI 视频拉片 | 查询任务与获取 JSON/Markdown 报告 | `GET /v1/video-reviews/jobs/{job_id}`、`GET /v1/video-reviews/jobs/{job_id}/report` |

内部上传、GPU 管理、音色修改和旧版 TTS 接口不在第三方 OpenAPI 中展示，公网访问固定返回 404。

## 三点五、AI 视频拉片与拆审

该接口默认不使用参考片、不计算单一总分，输出镜头时间码、关键帧、画面观察、声音/字幕状态、问题证据、严重度、置信度和返修建议。参考图或参考视频只有在 `continuity_check=true` 时才用于人物/产品/风格连续性对照。

```bash
curl -sS -X POST "$Base/v1/video-reviews/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  --data-raw '{
    "video_url": "https://storage.example.com/ad.mp4",
    "language": "auto",
    "analysis_profile": "advertising",
    "include_audio": true,
    "include_transcript": true,
    "continuity_check": false,
    "reference_assets": [],
    "external_ref": "review-001",
    "metadata": {}
  }'
```

返回 `job_id` 后查询：

```bash
curl -sS "$Base/v1/video-reviews/jobs/JOB_ID" \
  -H "Authorization: Bearer $SERVICE_TOKEN"

curl -sS "$Base/v1/video-reviews/jobs/JOB_ID/report?format=markdown" \
  -H "Authorization: Bearer $SERVICE_TOKEN" -o video-review.md
```

网页报告使用 `?format=html`，结构化数据使用默认 `?format=json`。

视频最大 10 分钟。默认报告不使用参考素材，避免参考片过拟合；任务详情中的每个问题都包含开始/结束时间、证据、影响、严重度、置信度和最小返修建议。

## 三、Windows PowerShell 可直接执行示例

先设置四个变量。请替换 Token 和两个素材 URL：

```powershell
$Base = "https://aicentre2.sligenai.cn:8443"
$Token = "替换为分配给你的SERVICE_TOKEN"
$VideoUrl = "https://你的对象存储.example.com/女1.mp4?签名参数"
$AudioUrl = "https://你的对象存储.example.com/女1_前10秒.mp3?签名参数"
$Headers = @{ Authorization = "Bearer $Token" }
```

### 3.1 语音识别：得到 10 秒音频文本

`language` 可省略或传 `auto`，两种写法都会让 ASR 自动识别语种；已知语种时仍可传 `zh`、`en` 等明确代码。

```powershell
$AsrBody = @{
  file_url = $AudioUrl
  language = "auto"
  beam_size = 5
} | ConvertTo-Json

$Asr = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/asr/transcriptions" `
  -Headers $Headers -ContentType "application/json" -Body $AsrBody `
  -TimeoutSec 900

$Asr.segments | Select-Object start, end, text
$Asr | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 ".\女1_前10秒_ASR.json"
```

使用 `curl.exe` 的同等请求：

```powershell
$Json = @{file_url=$AudioUrl; language="auto"; beam_size=5} | ConvertTo-Json -Compress
curl.exe -sS -X POST "$Base/v1/asr/transcriptions" `
  -H "Authorization: Bearer $Token" `
  -H "Content-Type: application/json" `
  --data-raw $Json
```

### 3.2 唇形驱动：MuseTalk + GFPGAN

```powershell
$LipBody = @{
  video_url = $VideoUrl
  audio_url = $AudioUrl
  face_restore = $true
} | ConvertTo-Json

$Created = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/lipsync/jobs" `
  -Headers $Headers -ContentType "application/json" -Body $LipBody `
  -TimeoutSec 900

$JobId = $Created.job_id
$Created
```

轮询到完成并下载视频：

```powershell
do {
  Start-Sleep -Seconds 3
  $Job = Invoke-RestMethod -Uri "$Base/v1/lipsync/jobs/$JobId" -Headers $Headers
  Write-Host "$($Job.state) / $($Job.stage)"
} while ($Job.state -in @("queued", "running"))

if ($Job.state -eq "completed") {
  Invoke-WebRequest -Uri $Job.result_url -OutFile ".\女1_唇形驱动_GFPGAN.mp4"
} else {
  throw "任务未成功：$($Job.error)"
}
```

任务完成后，`result_url` 是可直接访问的 OSS HTTPS 地址：

```json
{
  "state": "completed",
  "result_url": "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/ai-centre/lipsync/{job_id}/result.mp4",
  "result_storage": "oss"
}
```

原鉴权下载接口 `GET /v1/lipsync/jobs/{job_id}/video` 继续保留作为兜底。

其他唇形任务接口：

```text
GET  /v1/lipsync/jobs?limit=50&state=completed
GET  /v1/lipsync/jobs/{job_id}/logs?stage=musetalk&tail=200
GET  /v1/lipsync/jobs/{job_id}/logs?stage=gfpgan&tail=200
POST /v1/lipsync/jobs/{job_id}/cancel
```

## 四、Python 可直接执行：ASR + 唇形 + 轮询 + 下载

安装依赖：`pip install requests`。替换三个变量后运行：

```python
import json
import time
from pathlib import Path

import requests

BASE = "https://aicentre2.sligenai.cn:8443"
TOKEN = "替换为分配给你的SERVICE_TOKEN"
VIDEO_URL = "https://你的对象存储.example.com/女1.mp4?签名参数"
AUDIO_URL = "https://你的对象存储.example.com/女1_前10秒.mp3?签名参数"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

asr = requests.post(
    f"{BASE}/v1/asr/transcriptions",
    headers=HEADERS,
    json={"file_url": AUDIO_URL, "language": "auto", "beam_size": 5},
    timeout=900,
)
asr.raise_for_status()
Path("女1_前10秒_ASR.json").write_text(
    json.dumps(asr.json(), ensure_ascii=False, indent=2), encoding="utf-8"
)
print("识别文本：", "".join(x["text"] for x in asr.json()["segments"]))

created = requests.post(
    f"{BASE}/v1/lipsync/jobs",
    headers=HEADERS,
    json={"video_url": VIDEO_URL, "audio_url": AUDIO_URL, "face_restore": True},
    timeout=900,
)
created.raise_for_status()
job_id = created.json()["job_id"]
print("任务 ID：", job_id)

while True:
    status = requests.get(
        f"{BASE}/v1/lipsync/jobs/{job_id}", headers=HEADERS, timeout=30
    )
    status.raise_for_status()
    job = status.json()
    print(job["state"], job.get("stage"))
    if job["state"] not in {"queued", "running"}:
        break
    time.sleep(3)

if job["state"] != "completed":
    raise RuntimeError(job.get("error") or "唇形任务失败")

with requests.get(
    job["result_url"],
    timeout=900,
    stream=True,
) as response:
    response.raise_for_status()
    with open("女1_唇形驱动_GFPGAN.mp4", "wb") as output:
        for chunk in response.iter_content(1024 * 1024):
            output.write(chunk)
```

## 五、语音合成

### 5.1 普通语音合成

中文接口名：**普通语音合成**  
接口：`POST /v2/tts/speech`

```powershell
$Body = @{
  text = "您好，这是 AI Centre 2 的语音合成示例。"
  voice_profile_id = "default"
  provider = "auto"
  prosody = @{ speed=1.0; volume=1.0; pitch=1.0 }
} | ConvertTo-Json -Depth 5

Invoke-WebRequest -Method Post -Uri "$Base/v2/tts/speech" `
  -Headers $Headers -ContentType "application/json" -Body $Body `
  -OutFile ".\普通语音合成.wav" -TimeoutSec 900
```

### 5.2 VoxCPM2 深度语音克隆

提供 `reference_audio_url` 时会强制使用 VoxCPM2；参考音频是可选字段。`clone_mode` 支持 `auto | controllable | ultimate`，`emotion_strategy` 支持 `auto | inherit | force`，二者默认均为 `auto`。系统会识别参考音频与正文语言：同语言且有准确参考文本时允许 Ultimate；跨语言固定使用隔离 Reference/Controllable 模式，`prompt_text` 只用于语言检测和审计，绝不会送入 continuation。参考音频会统一为 16kHz 单声道，并从长参考中自动选择最具代表性的约 5 秒。

情绪与参考一致时默认继承参考表达，不重复注入风格；明显冲突或显式 `emotion_strategy=force` 时才使用目标情绪，可能降低音色相似度。跨语言同步请求先生成常规候选，只有正文、首尾或 ERes2NetV2 说话人门禁未通过时才追加受控 seed，最多六个候选；隔离参考的中文中长句会按逗号短语分段，减少音色漂移。系统按正文 CER、发音 CER、说话人相似度、情绪和首尾异常选择最佳结果，不使用会引入额外发声的中文前导词。全部未过门禁仍返回综合最佳结果并标记 `X-TTS-Quality-Passed: false`。`prosody.speed`、`volume`、`pitch` 均真实处理音频。

```powershell
$CloneBody = @{
  text = "这段文字将使用参考音频进行深度语音克隆。"
  voice_profile_id = "default"
  reference_audio_url = $AudioUrl
  prompt_text = "参考音频中准确说出的文字"
  emotion = "真诚、温暖、有感染力"
  emotion_enhance = $false
  clone_mode = "auto"
  emotion_strategy = "auto"
  prosody = @{ speed=1.0; volume=1.0; pitch=1.0 }
  quality_mode = "standard"
} | ConvertTo-Json

Invoke-WebRequest -Method Post -Uri "$Base/v2/tts/speech" `
  -Headers $Headers -ContentType "application/json" -Body $CloneBody `
  -OutFile ".\VoxCPM2_克隆.wav" -TimeoutSec 900
```

响应头会返回实际路由：`X-TTS-Clone-Mode`、`X-TTS-Clone-Fallback`、`X-TTS-Reference-Language`、`X-TTS-Target-Language`、`X-TTS-Emotion-Strategy`、`X-TTS-Candidate-Count`、`X-TTS-Reference-Window` 和 `X-TTS-Reference-Fallback`。显式请求 Ultimate 但检测到跨语言时不会报 422，而会自动降级，并通过 `X-TTS-Clone-Fallback: cross-language-ultimate-disabled` 说明原因。

### 5.3 实时 PCM 流式合成

`POST /v2/tts/speech/stream` 使用与同步接口相同的 JSON。流式接口只支持 `quality_mode=standard`，跨语言使用处理后的最佳参考片段单次生成，不执行三候选择优；返回 48kHz、16-bit、小端、单声道 PCM。响应头 `X-TTS-Request-ID` 可用于查询最终审计。

```bash
curl -N -X POST "$BASE/v2/tts/speech/stream" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"这是实时流式语音。","emotion":"自然、亲切","prosody":{"speed":1.1,"volume":1.0,"pitch":1.0}}' \
  --output speech.pcm

ffplay -f s16le -ar 48000 -ac 1 speech.pcm
```

```http
GET /v2/tts/quality/{X-TTS-Request-ID}
```

### 5.4 最多20000字符的异步长语音

同步与流式接口仍限制5000字符。`POST /v2/tts/jobs` 支持1～20000字符，服务端按中文最多120字符、拉丁文本最多300字符智能分段，最多并行预生成2段，按原顺序加入句间/段间停顿并合成一个WAV。任务完成后同时提供本地鉴权下载接口和公网OSS `audio_url`。

异步长语音支持普通TTS，也支持与同步接口相同的 `reference_audio_url`、`prompt_text`、`emotion`、`emotion_enhance`、`clone_mode`、`emotion_strategy` 和 `prosody`。长语音固定使用 `quality_mode=standard`，避免对整段执行三次阻塞式生成与ASR；失败分段会独立重试，不会重新生成前面已经完成的分段。

```bash
python - <<'PY'
import json
text = "这是需要合成的长文本。" * 800
with open("long-tts-request.json", "w", encoding="utf-8") as file:
    json.dump({
        "text": text,
        "language": "zh",
        "voice_profile_id": "default",
        "provider": "auto",
        "reference_audio_url": "https://你的对象存储.example.com/reference.MP3",
        "prompt_text": "参考音频中准确说出的文字",
        "emotion": "自然、连贯、温暖",
        "emotion_enhance": False,
        "quality_mode": "standard",
        "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0},
        "idempotency_key": "order-20260820-long-001"
    }, file, ensure_ascii=False)
PY

curl -X POST "$BASE/v2/tts/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @long-tts-request.json
```

异步接口：

```text
POST /v2/tts/jobs
GET  /v2/tts/jobs/{job_id}
GET  /v2/tts/jobs/{job_id}/audio
GET  /v2/tts/providers
GET  /v2/tts/voices
```

创建任务返回202和 `job_id`。查询运行中任务时，`result` 会包含 `stage`、`progress`、`completed_segments` 和 `total_segments`；完成后 `result.audio_url` 是可直接访问的OSS WAV地址，`result.segment_count` 是实际分段数。`idempotency_key` 必须为8～256字符，相同值重复提交会返回同一个任务，避免重复计费。

## 六、OCR 文字识别

中文接口名：**通过图片 URL 批量识别文字**  
接口：`POST /v1/ocr/batch`

```powershell
$OcrBody = @{
  job_id = "order-20260802-001"
  source_lang_hint = "zh"
  images = @(
    @{ image_id="front"; url="https://你的对象存储.example.com/front.png"; regions=@() },
    @{ image_id="back"; url="https://你的对象存储.example.com/back.jpg"; regions=@(
      @{ name="title"; bbox=@(10,20,800,300) }
    )}
  )
} | ConvertTo-Json -Depth 10

$Ocr = Invoke-RestMethod -Method Post -Uri "$Base/v1/ocr/batch" `
  -Headers $Headers -ContentType "application/json" -Body $OcrBody -TimeoutSec 900
$Ocr | ConvertTo-Json -Depth 20
```

## 七、人脸处理

推荐使用长连接接口，客户端无需轮询。服务端内部仍以高优先级异步任务执行，请把客户端请求超时设置为至少 1800 秒。成功响应直接返回可访问的 OSS `video_url`：

```powershell
$FaceBody = @{
  source_uri = $VideoUrl
  filename = "face_mosaic.mp4"
  external_ref = "order-20260802-001"
  metadata = @{}
} | ConvertTo-Json

$FaceResult = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/face-mosaic/jobs/wait" `
  -Headers $Headers -ContentType "application/json" -Body $FaceBody `
  -TimeoutSec 1800

$FaceResult.video_url
```

示例响应：

```json
{
  "job_id": "0b3351a5-988a-4401-af51-fcc216d38f41",
  "status": "succeeded",
  "video_url": "https://oss.example.com/path/face_mosaic.mp4",
  "applied": true,
  "analysis": {}
}
```

如果网关等待达到 1800 秒会返回 504，并附带 `job_id`；后台任务不会取消，仍可通过原查询接口获取结果。

原异步接口继续保留：

中文接口名：**通过视频 URL 创建人脸处理任务**  
接口：`POST /v1/face-mosaic/jobs`

```powershell
$FaceBody = @{
  source_uri = $VideoUrl
  filename = "face_mosaic.mp4"
  external_ref = "order-20260802-001"
  metadata = @{}
} | ConvertTo-Json

$Face = Invoke-RestMethod -Method Post -Uri "$Base/v1/face-mosaic/jobs" `
  -Headers $Headers -ContentType "application/json" -Body $FaceBody -TimeoutSec 900
$Face
```

```text
GET  /v1/face-mosaic/jobs/{job_id}
POST /v1/face-mosaic/jobs/{job_id}/cancel
```

## 八、SceneDetect 视频切片

输入为公网 HTTPS 视频 URL。默认使用 ContentDetector，`threshold=27.0`、`min_scene_len=15` 帧。结果中的每个 `video_url` 都是可直接访问的 OSS HTTPS 地址。

### 8.1 普通异步任务（优先级 5）

```powershell
$SceneBody = @{
  source_uri = $VideoUrl
  filename = "scene.mp4"
  threshold = 27.0
  min_scene_len = 15
  external_ref = "order-20260803-001"
  metadata = @{}
} | ConvertTo-Json

$SceneJob = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/video-scenes/jobs" `
  -Headers $Headers -ContentType "application/json" -Body $SceneBody

$SceneJob.job_id
```

查询与取消：

```text
GET  /v1/video-scenes/jobs/{job_id}
POST /v1/video-scenes/jobs/{job_id}/cancel
```

### 8.2 提交并等待结果（优先级 9，推荐需要直接结果时使用）

服务端仍通过 Celery 异步执行，但当前 HTTP 请求会等待完成；客户端超时建议至少 1800 秒。

```powershell
$SceneResult = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/video-scenes/jobs/wait" `
  -Headers $Headers -ContentType "application/json" -Body $SceneBody `
  -TimeoutSec 1800

$SceneResult.scenes | Select-Object index,start_seconds,end_seconds,duration_seconds,video_url
```

成功响应：

```json
{
  "job_id": "任务UUID",
  "status": "succeeded",
  "scene_count": 2,
  "scenes": [
    {
      "index": 1,
      "start_frame": 0,
      "end_frame": 125,
      "start_seconds": 0.0,
      "end_seconds": 5.0,
      "duration_seconds": 5.0,
      "video_url": "https://oss.example.com/path/scene_001.mp4"
    }
  ],
  "elapsed_seconds": 8.31
}
```

如果等待达到 1800 秒，接口返回 504 和 `job_id`，后台切片任务不会被取消，可继续查询。

## 九、视频水印处理

输入为公网 HTTPS 视频 URL，输出为 OSS HTTPS 视频 URL。`mode` 可选 `light` 或
`intensive`；`keep_intermediates` 默认 `false`，任务结束后立即删除中间文件。设为
`true` 时，中间文件仅在 worker 本地保留，并在后续任务启动时清理超过 24 小时的文件。

```powershell
$WatermarkBody = @{
  source_uri = $VideoUrl
  filename = "watermark_removed.mp4"
  mode = "intensive"
  keep_intermediates = $false
  external_ref = "order-20260807-001"
  metadata = @{}
} | ConvertTo-Json
```

### 9.1 异步任务

```powershell
$WatermarkJob = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/watermark-removal/jobs" `
  -Headers $Headers -ContentType "application/json" -Body $WatermarkBody

$WatermarkJob.job_id
```

查询与取消：

```text
GET  /v1/watermark-removal/jobs/{job_id}
POST /v1/watermark-removal/jobs/{job_id}/cancel
```

### 9.2 同步等待

```powershell
$WatermarkResult = Invoke-RestMethod -Method Post `
  -Uri "$Base/v1/watermark-removal/jobs/wait" `
  -Headers $Headers -ContentType "application/json" -Body $WatermarkBody `
  -TimeoutSec 3600

$WatermarkResult.video_url
```

成功响应：

```json
{
  "job_id": "任务UUID",
  "status": "succeeded",
  "mode": "intensive",
  "video_url": "https://oss.example.com/path/watermark_removed.mp4",
  "intermediates_retained": false,
  "elapsed_seconds": 27.1
}
```

同步等待超时会返回 504 和 `job_id`，后台任务不会被取消，可继续通过状态接口查询。

## 十、DA2 / DA3 单目视频深度推理

支持 DA2（Video Depth Anything）和 Depth Anything 3，各自可选 Small/Base。输出为时序一致的灰度深度 MP4，并上传为可直接访问的 OSS HTTPS URL。公开接口只接收公网 HTTPS 视频 URL。

```bash
curl -X POST "$BASE/v1/video-depth/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "version": "da2",
    "model": "small",
    "filename": "depth.mp4",
    "input_size": 518,
    "max_resolution": 960,
    "target_fps": -1
  }'
```

- `version`：`da2` 或 `da3`，默认 `da2`。`da2` 是现有 Video Depth Anything 时序管线。
- `model`：`small` 或 `base`，默认 `small`。Small 速度更快且显存更低，Base 细节更强。
- `input_size`：模型输入尺寸，必须是14的倍数，范围224～756，默认518。
- 生产 GPU1 上 DA2 Base 的 `input_size` 安全上限为392；传入更大值会自动降为392，并通过 `requested_input_size`、`effective_input_size`、`input_size_capped` 明确返回。DA2 Small 与 DA3 不应用此上限。
- `max_resolution`：输入视频最长边限制，范围224～1920，默认960。
- `target_fps`：`-1`保持原帧率，或传1～60进行抽帧。
- 异步查询：`GET /v1/video-depth/jobs/{job_id}`。
- 取消任务：`POST /v1/video-depth/jobs/{job_id}/cancel`。
- 直接等待结果：将提交地址改为 `/v1/video-depth/jobs/wait`，客户端超时建议3600秒。

成功结果包含实际使用的 `version`、`model`、`model_name`，以及 `video_url`、`frame_count`、`fps`、`duration_seconds`、`inference_seconds`、`peak_vram_gb` 和 `elapsed_seconds`。

## 十一、音频分离

使用 Bandit v2 DnR v3 Multilingual 将音频或视频分成影视对白、音乐、音效，并额外返回 `background = music + sfx`。公开接口只接受公网HTTPS URL，支持MP4、MOV、MKV、WebM、WAV、MP3、M4A、AAC、FLAC和OGG。

```bash
curl -X POST "$BASE/v1/audio-separation/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "model": "bandit-v2-multilingual",
    "filename_prefix": "campaign-001",
    "external_ref": "order-20260820-001",
    "metadata": {}
  }'
```

异步提交返回HTTP 202和 `job_id`。任务管理：

```text
GET  /v1/audio-separation/jobs/{job_id}
POST /v1/audio-separation/jobs/{job_id}/cancel
```

需要保持连接直接等待结果时，将地址改为：

```text
POST /v1/audio-separation/jobs/wait

## 视频超分

默认使用智能渠道，依次尝试 FlashVSR V2、FlashVSR、SeedVR2。源视频自动切成不超过12秒的片段，默认最多3片并发超分；某个片段失败时只重新生成该片段并切换渠道，不会重跑已成功片段。全部片段成功后按原顺序无转场合并、恢复原始音轨并上传最终结果。指定具体 `provider` 时会在同一渠道最多重试3次。

```bash
curl -X POST "$BASE/v1/video-upscale/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_uri": "https://storage.example.com/video/source.mp4",
    "provider": "auto",
    "max_resolution": 1920,
    "external_ref": "order-001",
    "metadata": {}
  }'
```

`provider`支持：`auto`、`flashvsr_v2`、`flashvsr`、`seedvr2`。`max_resolution`范围480～3840；SeedVR2将该值作为目标最短边，FlashVSR系列作为最大分辨率参数。

```http
GET  /v1/video-upscale/jobs/{job_id}
POST /v1/video-upscale/jobs/{job_id}/cancel
POST /v1/video-upscale/jobs/wait
```

成功结果中的`provider`是最终实际渠道；不同片段使用不同渠道时为`mixed`。`fallback_used`表示是否发生片段重试，`segments`展示每个片段的渠道和尝试次数，不包含第三方Token、远端任务ID或中间素材URL。
```

成功结果示例：

```json
{
  "job_id": "4c39cf01-9893-436e-9378-1be045d98f64",
  "status": "succeeded",
  "stage": "completed",
  "model": "bandit-v2-multilingual",
  "speech_url": "https://oss.example.com/audio/campaign-001_speech.wav",
  "music_url": "https://oss.example.com/audio/campaign-001_music.wav",
  "sfx_url": "https://oss.example.com/audio/campaign-001_sfx.wav",
  "background_url": "https://oss.example.com/audio/campaign-001_background.wav",
  "duration_seconds": 32.829,
  "sample_rate": 48000,
  "channels": 2,
  "inference_seconds": 3.391,
  "rtf": 0.1033,
  "reconstruction_snr_db": 48.34,
  "peak_cuda_allocated_mib": 6907.91,
  "elapsed_seconds": 8.12
}
```

- `speech_url` 是影视对白，不保证把歌唱声归入该轨；歌曲人声通常进入 `music_url`。
- `background_url` 是音乐和音效的组合，适合直接获得“去对白背景”。
- 四个结果均为48kHz双声道PCM WAV，交付前执行-1dBFS样本峰值保护。
- 单文件上限512MiB；GPU1 worker并发固定为1，任务可在Redis队列中排队。
- 本次15条多语言实测RTF P95为0.158；泰语强干扰和强音乐中文素材仍建议人工抽听。

## 十二、通用原子视频生成（Seedance 2.0）

Seedance 2.0是一个逻辑模型，可由后台绑定jmapi和libtv渠道。单次请求只生成一个片段，不负责分段或Agent编排。

```bash
curl -X POST "$BASE/v1/video-generations/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"seedance-2.0",
    "channel":"auto",
    "prompt":"产品放置在水面上，镜头缓慢推进。",
    "reference_image_urls":["https://storage.example.com/product.png"],
    "reference_video_urls":[],
    "reference_audio_urls":[],
    "duration_seconds":5,
    "resolution":"720p",
    "aspect_ratio":"9:16",
    "sound":false
  }'
```

- `channel`省略时默认`jmapi`；传`auto`时才允许在可重试故障后切换到libtv。
- 明确传`jmapi`或`libtv`时不切换渠道。
- jmapi首版支持图片参考；libtv首版支持视频参考。渠道未配置或未通过健康检查时不会受理任务。
- `model`也接受`seedance-2.5`：时长4～30秒（超过15秒只有它接），参考素材上限30图/10视频且参考视频音频合计≤30秒；分辨率上jmapi只有480p/720p（1080p会自动落到libtv），480p只有2.5能做。

```http
GET  /v1/video-generations/jobs/{job_id}
POST /v1/video-generations/jobs/{job_id}/cancel
```

## 十三、MiniMax H3多机视频生成

该接口提供单次原子视频生成，支持纯文本及公网HTTPS参考素材URL；固定使用受控H3工作流，不接受ComfyUI工作流JSON。多段拆分、连续性和业务编排由调用方Agent负责。任务由AI Centre自动分配到在线空闲Worker；运行中的Worker失联时最多转移2次。Prompt不会被中台追加或改写。

```bash
curl -X POST "$BASE/v1/video-generations/minimax-h3/jobs" \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只雄鹰在黑夜中飞过雪山，电影级光影",
    "duration_seconds": 5,
    "quality": "medium",
    "resolution": "480p",
    "aspect_ratio": "9:16",
    "priority": 500,
    "seed": 482901731,
    "external_ref": "order-001",
    "metadata": {}
  }'
```

- `reference_image_urls`、`reference_video_urls`、`reference_audio_urls`均为可选URL列表；全部不传时为纯文本生成。
- `prompt`为必填字符串；每次请求只生成一个片段，不支持`segment_mode`、`prompts`或`split_seconds`。
- `duration_seconds`范围2～15秒，默认5秒。需要多段时由上层Agent分别提交多个原子任务并负责连续性和剪辑。
- `resolution`支持`480p | 720p | 1080p`，默认`480p`；`aspect_ratio`支持`9:16 | 16:9 | 1:1 | 4:3 | 3:4`，默认`9:16`。
- `quality`支持`low | medium | high`，默认`medium`：`low`走4步Euler快速预览和较小内部画布；`medium`走4步Euler原生安全画布；`high`走4步`res_multistep/simple`质量采样，耗时明显增加。兼容旧拼写`midia`并归一化为`medium`。
- `priority`范围为`1～1000`，默认`500`，数值越高越先获得下一台空闲Worker；同优先级按提交时间先后处理。优先级不会中断已经在GPU上运行的任务。
- 720P/1080P使用安全模型尺寸生成，再一次性缩放到标准交付尺寸，避免远程5090显存溢出。旧调用方仍可同时传`width`和`height`使用自定义模型尺寸。
- 参考视频有音轨时自动连接同编号视频音频；独立参考音频连接`<Audio 1>`。没有视频音轨时不会建立空音轨引用。
- 视频上限512MiB，身份图上限20MiB。Worker地址、ComfyUI任务ID和内部路径不会出现在响应中。

任务管理：

```text
GET  /v1/video-generations/minimax-h3/jobs/{job_id}
GET  /v1/video-generations/minimax-h3/jobs/{job_id}/queue-position
GET  /v1/video-generations/minimax-h3/workers/status
POST /v1/video-generations/minimax-h3/jobs/{job_id}/cancel
```

排队位置接口返回`ahead_count`（前面等待的任务数）、`position`（从1开始的位置）、`queued_total`及任务优先级；任务已经运行或结束时`queued=false`且`position=null`。Worker状态接口返回`online`、`idle`、`busy`、`offline`、`disabled`、`draining`、`incompatible`和`unknown`数量，其中`online`是当前可连接的空闲、忙碌及排空机器总数。

完成后`result_url`是可直接访问的OSS成片，`output`返回实际质量档位、交付宽高、模型宽高、比例和目标时长；`attempt_count`表示实际执行次数。接口仅提供异步原子任务。

## 十三、状态码与重试

| 状态码 | 含义 | 处理建议 |
|---:|---|---|
| 200 / 202 | 成功 / 已接收 | 正常处理 |
| 400 | 素材为空或业务参数无效 | 修正素材，不重试 |
| 401 | Token 缺失或错误 | 更新凭证 |
| 404 | 接口或任务不存在 | 检查地址和任务 ID |
| 409 | 结果尚未就绪 | 延迟后重新查询 |
| 413 | 素材超过限制 | 压缩素材 |
| 415 | URL 后缀或 Content-Type 不支持/冲突 | 转换格式 |
| 422 | URL 非 HTTPS、指向非公网地址或字段无效 | 修正请求 |
| 502 | 素材下载或推理上游失败 | 有限退避重试 |
| 503 / 504 | 服务不可用或下载超时 | 按 2、5、10 秒退避重试并告警 |

不要在错误日志中记录完整签名 URL。客户端总超时建议不少于 900 秒；异步任务的状态查询建议每 3 秒一次。
