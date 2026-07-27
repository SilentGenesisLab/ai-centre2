# AI Centre 2 API 使用说明

本文档对应 AI Centre 2 Control Plane `2.0.0`，服务器部署目录：

```text
/home/donxu/ai-centre
```

## 1. 连接信息

控制服务默认只监听服务器本机：

```text
http://127.0.0.1:8320
```

这意味着：

- 在服务器内部调用时，直接使用以上地址。
- 在 Windows 本地调用时，先建立 SSH 隧道。
- 不应直接把 8320 端口暴露到公网。

### 1.1 Windows 建立 SSH 隧道

保持以下 PowerShell 窗口运行：

```powershell
ssh -N -L 8320:127.0.0.1:8320 -p 2222 donxu@121.15.184.231
```

之后本地程序使用：

```text
http://127.0.0.1:8320
```

### 1.2 在线接口结构

建立 SSH 隧道后可以打开：

- Swagger UI：<http://127.0.0.1:8320/docs>
- OpenAPI JSON：<http://127.0.0.1:8320/openapi.json>
- 健康检查：<http://127.0.0.1:8320/health>

## 2. 鉴权

除 `/health`、`/docs` 和 `/openapi.json` 外，本文档中的业务接口都需要：

```http
Authorization: Bearer <SERVICE_TOKEN>
```

服务器上的 token 来自：

```text
/home/donxu/ai-centre/.env
```

在服务器 Shell 中安全读取：

```bash
cd /home/donxu/ai-centre
set -a
source .env
set +a
```

请勿把真实 token 写入 Git、日志、截图或前端代码。

未传 token 或 token 错误时返回：

```json
{
  "detail": "invalid service token"
}
```

HTTP 状态码为 `401`。

## 3. 健康检查

### `GET /health`

不需要鉴权。检查控制面、ASR、TTS 和被管理 GPU worker 的状态。

```bash
curl http://127.0.0.1:8320/health
```

响应示例：

```json
{
  "status": "ok",
  "upstreams": {
    "asr": {
      "status": "ok",
      "url": "http://127.0.0.1:9001",
      "details": {
        "status": "ok",
        "model": "large-v3",
        "device": "cuda",
        "compute": "float16"
      }
    },
    "tts": {
      "status": "ok",
      "url": "http://127.0.0.1:8191",
      "details": {
        "status": "ok",
        "cuda": true,
        "loaded": true
      }
    }
  },
  "gpus": []
}
```

顶层 `status`：

- `ok`：ASR 和 TTS 上游均可访问。
- `degraded`：至少一个音频上游不可用。

GPU0 的 OCR/人脸 worker 被主动关闭时，OCR 网关自身可能显示 `degraded`，但不影响本接口根据 ASR/TTS 返回 `ok`。

## 4. ASR 语音识别

### `POST /v1/asr/transcriptions`

上传音频或带音轨的视频，返回 Faster-Whisper 转写结果。

Content-Type：

```text
multipart/form-data
```

参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---:|---:|---:|---|
| `file` | 文件 | 是 | - | 音频或上游支持的视频文件 |
| `language` | string | 否 | 自动检测 | ISO 639-1 语言代码，如 `es`、`pt`、`th`、`en`、`zh` |
| `beam_size` | integer | 否 | `5` | 搜索宽度，范围 `1`～`10`；越大通常越慢 |

### 4.1 Linux/macOS curl

```bash
curl -X POST "http://127.0.0.1:8320/v1/asr/transcriptions" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  -F "file=@/data/input.mp4" \
  -F "language=es" \
  -F "beam_size=5"
```

自动识别语言时不要传 `language`：

```bash
curl -X POST "http://127.0.0.1:8320/v1/asr/transcriptions" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  -F "file=@/data/input.wav"
```

### 4.2 Windows PowerShell

PowerShell 中建议显式调用 `curl.exe`：

```powershell
$token = "替换为SERVICE_TOKEN"

curl.exe -X POST "http://127.0.0.1:8320/v1/asr/transcriptions" `
  -H "Authorization: Bearer $token" `
  -F "file=@I:\AI-video\input.wav" `
  -F "language=es" `
  -F "beam_size=5"
```

### 4.3 Python

```python
from pathlib import Path

import requests

base_url = "http://127.0.0.1:8320"
token = "替换为SERVICE_TOKEN"
audio_path = Path(r"I:\AI-video\input.wav")

with audio_path.open("rb") as audio:
    response = requests.post(
        f"{base_url}/v1/asr/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (audio_path.name, audio, "application/octet-stream")},
        data={"language": "es", "beam_size": 5},
        timeout=900,
    )

response.raise_for_status()
result = response.json()
print(result["text"])
print(result["segments"])
```

### 4.4 响应示例

实际字段由当前 ASR 后端返回：

```json
{
  "text": "Texto reconocido.",
  "language": "es",
  "language_probability": 0.998,
  "duration": 12.48,
  "elapsed": 1.72,
  "segments": [
    {
      "start": 0.42,
      "end": 2.84,
      "text": "Texto reconocido."
    }
  ]
}
```

## 5. TTS 普通语音合成

### `POST /v1/tts/speech`

没有传参考音频路径时，接口自动调用 VoxCPM2 普通 TTS。

Content-Type：

```text
application/json
```

请求字段：

| 字段 | 类型 | 必填 | 默认值 | 约束/说明 |
|---|---:|---:|---:|---|
| `text` | string | 是 | - | 1～5000 字符 |
| `cfg_value` | float | 否 | `2.0` | 范围 `1.0`～`3.0` |
| `inference_timesteps` | integer | 否 | `10` | 范围 `1`～`50`；越大通常越慢 |

### 5.1 curl

```bash
curl -X POST "http://127.0.0.1:8320/v1/tts/speech" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Esta es una prueba de voz.",
    "cfg_value": 2.0,
    "inference_timesteps": 10
  }' \
  --output speech.wav
```

### 5.2 Windows PowerShell

```powershell
$token = "替换为SERVICE_TOKEN"
$body = @{
    text = "Esta es una prueba de voz."
    cfg_value = 2.0
    inference_timesteps = 10
} | ConvertTo-Json

Invoke-WebRequest `
  -Uri "http://127.0.0.1:8320/v1/tts/speech" `
  -Method Post `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json" `
  -Body $body `
  -OutFile "speech.wav"
```

### 5.3 Python

```python
from pathlib import Path

import requests

response = requests.post(
    "http://127.0.0.1:8320/v1/tts/speech",
    headers={"Authorization": "Bearer 替换为SERVICE_TOKEN"},
    json={
        "text": "Esta es una prueba de voz.",
        "cfg_value": 2.0,
        "inference_timesteps": 10,
    },
    timeout=900,
)
response.raise_for_status()
Path("speech.wav").write_bytes(response.content)
```

成功时响应体是 WAV 二进制，不是 JSON。请直接保存为 `.wav`。

## 6. TTS 参考音色克隆

仍然调用：

```text
POST /v1/tts/speech
```

只要 `reference_wav_path` 或 `prompt_wav_path` 任意一个非空，控制面就会路由到 VoxCPM2 克隆接口。

额外字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---:|---:|---|
| `reference_wav_path` | string | 视后端要求 | 参考音频在服务器上的绝对路径 |
| `prompt_wav_path` | string | 视后端要求 | 提示音频在服务器上的绝对路径 |
| `prompt_text` | string | 否 | 参考音频对应文本 |

重要限制：

> 这里传的是服务器本地路径。Windows 的 `I:\...`、`J:\...` 路径不能直接传给 Linux 服务器。音频必须先上传或同步到服务器。

示例：

```bash
curl -X POST "http://127.0.0.1:8320/v1/tts/speech" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Esta es una prueba con voz clonada.",
    "reference_wav_path": "/home/donxu/ai-centre/runtime/references/speaker01.wav",
    "prompt_text": "Texto pronunciado en el audio de referencia.",
    "cfg_value": 2.0,
    "inference_timesteps": 10
  }' \
  --output cloned.wav
```

响应可能带有以下上游透传头：

- `Content-Disposition`
- `X-Elapsed-Seconds`
- `X-Audio-Duration`

## 7. GPU worker 管理

这些接口目前只管理以下 user-systemd 服务：

| GPU | 服务 |
|---:|---|
| GPU0 | `ai-centre-face-worker-gpu0.service`、`ai-centre-ocr-worker@0.service` |
| GPU1 | `ai-centre-face-worker-gpu1.service`、`ai-centre-ocr-worker@1.service` |

它们**不会**关闭：

- Faster-Whisper ASR
- VoxCPM2 TTS
- 其他用户部署的 CUDA 进程

因此，`disable GPU1` 不等于释放 GPU1 的全部显存。

目前只支持 `gpu_id=0` 和 `gpu_id=1`。其他编号返回 `404`。

### 7.1 查询状态

#### `GET /v1/admin/gpus`

```bash
curl "http://127.0.0.1:8320/v1/admin/gpus" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}"
```

响应示例：

```json
{
  "gpus": [
    {
      "gpu_id": 0,
      "enabled": false,
      "services": [
        {
          "name": "ai-centre-face-worker-gpu0.service",
          "query_ok": true,
          "ActiveState": "inactive",
          "SubState": "dead",
          "UnitFileState": "disabled"
        }
      ]
    }
  ]
}
```

### 7.2 临时排空 GPU

#### `POST /v1/admin/gpus/{gpu_id}/drain`

停止该 GPU 上被允许管理的 worker，但不修改 systemd 开机启用状态。服务器或用户服务重启后，它们可能再次启动。

```bash
curl -X POST "http://127.0.0.1:8320/v1/admin/gpus/0/drain" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}"
```

### 7.3 持久停用 GPU worker

#### `POST /v1/admin/gpus/{gpu_id}/disable`

执行 `disable --now`：立即停止，并阻止这些 worker 在下次 user-systemd 启动时自动运行。

```bash
curl -X POST "http://127.0.0.1:8320/v1/admin/gpus/0/disable" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}"
```

### 7.4 恢复 GPU worker

#### `POST /v1/admin/gpus/{gpu_id}/enable`

执行 `enable --now`：恢复自动启动并立即启动 worker。

```bash
curl -X POST "http://127.0.0.1:8320/v1/admin/gpus/0/enable" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}"
```

### 7.5 管理操作响应

```json
{
  "gpu_id": 0,
  "status": "disabled",
  "actions": [
    {
      "service": "ai-centre-face-worker-gpu0.service",
      "action": "disable",
      "ok": true,
      "error": null
    },
    {
      "service": "ai-centre-ocr-worker@0.service",
      "action": "disable",
      "ok": true,
      "error": null
    }
  ]
}
```

必须同时检查：

- 顶层 `status` 是否为预期值。
- 每一项 `actions[].ok` 是否为 `true`。

## 8. 常见 HTTP 状态码

| 状态码 | 含义 |
|---:|---|
| `200` | 请求成功 |
| `400` | 空音频文件等无效业务输入 |
| `401` | SERVICE_TOKEN 缺失或错误 |
| `404` | 不支持的 GPU 编号 |
| `422` | 参数类型、范围或必填字段校验失败 |
| `502` | ASR/TTS 上游调用失败或超时 |

ASR 上游失败示例：

```json
{
  "detail": "ASR backend failed: ..."
}
```

TTS 上游失败示例：

```json
{
  "detail": "TTS backend failed: ..."
}
```

控制面的上游超时默认是 900 秒。客户端超时建议设置为不低于 900 秒，批量系统应使用自己的任务队列，不应依靠无限 HTTP 连接。

## 9. 推荐的生产调用顺序

1. 调用 `/health`，确认目标上游是 `ok`。
2. 发起 ASR 或 TTS 请求。
3. 为每个请求记录业务 `job_id`、输入文件哈希、开始时间、HTTP 状态和耗时。
4. 只在确认没有该 GPU 上的活跃任务后执行 `drain` 或 `disable`。
5. 批处理程序对 `502` 做有限次数退避重试；不要对 `400`、`401`、`404`、`422` 自动重试。

当前接口是同步推理接口：连接会持续到识别或合成完成。真正的高并发批量作业应在其上方增加任务队列和状态查询层。
