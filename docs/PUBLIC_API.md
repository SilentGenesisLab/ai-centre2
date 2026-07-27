# AI Centre 2 公网 API 文档

## 1. 当前状态

| 项目 | 当前值 |
|---|---|
| 公网 IP | `121.15.184.231` |
| AI 服务器内网 IP | `192.168.31.23` |
| 控制服务内部地址 | `http://127.0.0.1:8320` |
| 计划公网地址 | `http://121.15.184.231:8320` |
| 公网 8320 状态 | **尚未开通** |

`http://121.15.184.231/` 当前打开的是小米路由器管理页面，不是 AI Centre 2。
不要把公网 IP 的 80 端口当作 API 地址。

## 2. 开通公网接口需要什么

公网请求需要经过下面两层：

```text
调用方
  -> 121.15.184.231:8320
  -> 小米路由器 TCP 端口映射
  -> 192.168.31.23:8320
  -> AI Centre 2
```

需要完成：

1. AI Centre 2 增加一个面向内网的监听入口。
2. 小米路由器增加 TCP 端口转发：

```text
公网端口：8320
内网主机：192.168.31.23
内网端口：8320
协议：TCP
```

3. 从真正的外网网络验证 `/health`。
4. 正式生产环境增加 HTTPS；不要长期通过明文 HTTP 发送 Bearer Token。

当前服务器没有 Nginx/Caddy，且 `donxu` 没有免密 sudo。路由器也没有开放 UPnP
端口管理。因此，公网映射需要持有小米路由器管理权限的人配置，不能只靠应用代码完成。

## 3. 公网 Base URL

完成上述映射后：

```text
http://121.15.184.231:8320
```

接口地址：

| 功能 | 方法 | 公网 URL |
|---|---|---|
| 健康检查 | `GET` | `http://121.15.184.231:8320/health` |
| Swagger | `GET` | `http://121.15.184.231:8320/docs` |
| OpenAPI | `GET` | `http://121.15.184.231:8320/openapi.json` |
| ASR | `POST` | `http://121.15.184.231:8320/v1/asr/transcriptions` |
| TTS | `POST` | `http://121.15.184.231:8320/v1/tts/speech` |
| GPU 状态 | `GET` | `http://121.15.184.231:8320/v1/admin/gpus` |
| 临时排空 GPU | `POST` | `http://121.15.184.231:8320/v1/admin/gpus/{gpu_id}/drain` |
| 停用 GPU worker | `POST` | `http://121.15.184.231:8320/v1/admin/gpus/{gpu_id}/disable` |
| 恢复 GPU worker | `POST` | `http://121.15.184.231:8320/v1/admin/gpus/{gpu_id}/enable` |

除 `/health`、`/docs` 和 `/openapi.json` 外，均需要：

```http
Authorization: Bearer <SERVICE_TOKEN>
```

## 4. 公网调用示例

### 4.1 健康检查

```bash
curl "http://121.15.184.231:8320/health"
```

### 4.2 ASR

```bash
curl -X POST "http://121.15.184.231:8320/v1/asr/transcriptions" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  -F "file=@/data/input.mp4" \
  -F "language=es" \
  -F "beam_size=5"
```

Windows PowerShell：

```powershell
$token = "替换为SERVICE_TOKEN"

curl.exe -X POST "http://121.15.184.231:8320/v1/asr/transcriptions" `
  -H "Authorization: Bearer $token" `
  -F "file=@I:\AI-video\input.wav" `
  -F "language=es" `
  -F "beam_size=5"
```

### 4.3 普通 TTS

```bash
curl -X POST "http://121.15.184.231:8320/v1/tts/speech" \
  -H "Authorization: Bearer ${SERVICE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Esta es una prueba de voz.",
    "cfg_value": 2.0,
    "inference_timesteps": 10
  }' \
  --output speech.wav
```

### 4.4 音色克隆 TTS

```bash
curl -X POST "http://121.15.184.231:8320/v1/tts/speech" \
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

`reference_wav_path` 是 AI 服务器上的路径，不能传调用方电脑的 Windows 路径。

## 5. Python 公网调用

```python
from pathlib import Path

import requests

base_url = "http://121.15.184.231:8320"
token = "替换为SERVICE_TOKEN"

with Path("input.wav").open("rb") as audio:
    response = requests.post(
        f"{base_url}/v1/asr/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("input.wav", audio, "application/octet-stream")},
        data={"language": "es", "beam_size": 5},
        timeout=900,
    )

response.raise_for_status()
print(response.json())
```

## 6. 上线安全要求

生产调用不建议直接长期使用：

```text
http://121.15.184.231:8320
```

原因是 HTTP 不加密，Bearer Token 和上传的音视频可能被网络中间节点读取。

推荐最终入口：

```text
https://api.<你的域名>/ai-centre2
```

至少应满足：

- HTTPS。
- 独立公网 API Token，不复用开发环境 token。
- 限制上传体积和请求速率。
- GPU 管理接口只允许固定办公 IP 或 VPN 访问。
- 业务 ASR/TTS 与 GPU 管理接口使用不同权限。
- 路由器关闭公网管理页面，避免公网 80 端口暴露设备后台。
- 日志不记录 Authorization 和完整敏感文本。

## 7. 映射后的验收命令

必须从手机热点、云服务器等真正的外网执行：

```bash
curl --connect-timeout 5 \
  "http://121.15.184.231:8320/health"
```

验收标准：

1. HTTP 状态为 `200`。
2. JSON 顶层 `status` 为 `ok` 或可解释的 `degraded`。
3. `upstreams.asr.status` 为 `ok`。
4. `upstreams.tts.status` 为 `ok`。
5. 不带 token 调用 ASR/TTS 返回 `401`。
6. 带正确 token 能完成一次真实 ASR 和 TTS。

完整字段、参数范围和错误码见 [API.md](API.md)。
