# AI Centre 2 音频分离 API 接入文档

版本：1.0  
更新日期：2026-08-20  
生产地址：`https://aicentre2.sligenai.cn:8443`

- 在线文档：`https://aicentre2.sligenai.cn:8443/admin/audio-separation-api.html`
- Swagger：`https://aicentre2.sligenai.cn:8443/docs`
- OpenAPI：`https://aicentre2.sligenai.cn:8443/openapi.json`

## 1. 功能说明

接口使用 Bandit v2 DnR v3 Multilingual，从音频或视频中提取：

| 返回字段 | 内容 |
|---|---|
| `speech_url` | 影视对白。歌唱通常进入音乐轨。 |
| `music_url` | 音乐。 |
| `sfx_url` | 环境声、转场声等音效。 |
| `background_url` | `music + sfx` 合成背景轨。 |

四个结果均为公网 HTTPS OSS URL，文件格式为48kHz、双声道、PCM16 WAV，并应用 `-1 dBFS` 样本峰值保护。

## 2. 鉴权与通用请求头

除健康检查外，所有请求都必须携带：

```http
Authorization: Bearer <SERVICE_TOKEN>
Content-Type: application/json
```

`<SERVICE_TOKEN>` 是占位符。实际 Token 由 AI Centre 管理员通过私聊或密码管理器单独提供，不应写入网页、Git、日志或截图。

```bash
export BASE='https://aicentre2.sligenai.cn:8443'
export SERVICE_TOKEN='粘贴管理员分配的Token'
```

## 3. 输入要求

- `source_uri` 必须是公网 HTTPS 音频或视频 URL，可以携带 OSS 签名查询参数。
- 不接受 `C:\...`、`K:\...` 等本地路径，也不接受公网 multipart 上传。
- 禁止 HTTP、本机、内网、云元数据地址、URL 用户名密码和 URL fragment。
- 最大文件为512MiB；下载、FFmpeg处理和推理存在超时保护。
- 支持视频：MP4、MOV、MKV、WebM。
- 支持音频：WAV、MP3、M4A、AAC、FLAC、OGG。
- 后缀不区分大小写，并结合响应 `Content-Type` 与文件头判断真实格式。

## 4. 创建异步音频分离任务

### 中文接口名

创建异步对白、音乐和音效分离任务

```http
POST /v1/audio-separation/jobs
```

### 请求字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `source_uri` | string | 是 | — | 公网 HTTPS 音频或视频 URL，最长4096字符。 |
| `model` | string | 否 | `bandit-v2-multilingual` | 当前只允许该值。 |
| `filename_prefix` | string | 否 | `separated` | 四轨结果文件名前缀，1～128字符，不能含 `/` 或 `\`。 |
| `external_ref` | string/null | 否 | `null` | 调用方订单号等外部引用，最长256字符。 |
| `run_id` | string/null | 否 | `null` | 调用方运行ID，最长256字符。 |
| `campaign_id` | string/null | 否 | `null` | 活动ID，最长256字符。 |
| `project_id` | string/null | 否 | `null` | 项目ID，最长256字符。 |
| `metadata` | object | 否 | `{}` | 调用方自定义结构化元数据。 |

未列出的字段会返回422，不会被静默忽略。

### 可直接执行的 curl

下面的示例素材已经过生产验证。执行前只需设置实际 `SERVICE_TOKEN`：

```bash
curl --location --request POST "$BASE/v1/audio-separation/jobs" \
  --header "Authorization: Bearer $SERVICE_TOKEN" \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/BE2.6/test4.wav",
    "model": "bandit-v2-multilingual",
    "filename_prefix": "order-001",
    "external_ref": "order-001",
    "metadata": {"business": "demo"}
  }'
```

### 202响应

```json
{
  "job_id": "0ca30251-7006-4663-a27b-813dc07a0089",
  "status": "queued",
  "priority": 5,
  "model": "bandit-v2-multilingual",
  "status_url": "/v1/audio-separation/jobs/0ca30251-7006-4663-a27b-813dc07a0089"
}
```

## 5. 查询任务

### 中文接口名

查询音频分离任务

```http
GET /v1/audio-separation/jobs/{job_id}
```

```bash
export JOB_ID='把创建接口返回的job_id粘贴到这里'

curl --location "$BASE/v1/audio-separation/jobs/$JOB_ID" \
  --header "Authorization: Bearer $SERVICE_TOKEN"
```

任务状态包括：`queued`、`running`、`retrying`、`succeeded`、`failed`、`cancel_requested`、`cancelled`。运行中响应还会包含 `stage` 和 `progress`。

### 完成响应

```json
{
  "job_id": "0ca30251-7006-4663-a27b-813dc07a0089",
  "status": "succeeded",
  "stage": "completed",
  "model": "bandit-v2-multilingual",
  "source_bytes": 906284,
  "speech_url": "https://oss.example.com/audio/order-001_speech.wav",
  "music_url": "https://oss.example.com/audio/order-001_music.wav",
  "sfx_url": "https://oss.example.com/audio/order-001_sfx.wav",
  "background_url": "https://oss.example.com/audio/order-001_background.wav",
  "duration_seconds": 10.274833,
  "sample_rate": 48000,
  "channels": 2,
  "inference_seconds": 2.0398,
  "rtf": 0.198524,
  "reconstruction_snr_db": 54.1578,
  "peak_limit_dbfs": -1.0,
  "elapsed_seconds": 11.231
}
```

`inference_seconds` 是模型纯推理时间；`elapsed_seconds` 还包含素材下载、格式转换、模型进程初始化和四个结果上传时间。

### Bash轮询示例

需要安装 `jq`：

```bash
while true; do
  RESULT="$(curl --silent --show-error \
    --header "Authorization: Bearer $SERVICE_TOKEN" \
    "$BASE/v1/audio-separation/jobs/$JOB_ID")"
  STATUS="$(printf '%s' "$RESULT" | jq -r '.status')"
  printf 'status=%s\n' "$STATUS"
  if [ "$STATUS" = 'succeeded' ] || [ "$STATUS" = 'failed' ] || [ "$STATUS" = 'cancelled' ]; then
    printf '%s\n' "$RESULT" | jq .
    break
  fi
  sleep 2
done
```

## 6. 提交并等待结果

### 中文接口名

高优先级提交音频分离并等待四轨 OSS 结果

```http
POST /v1/audio-separation/jobs/wait
```

请求体与异步接口完全相同。接口完成时直接返回第5节的成功响应：

```bash
curl --location --max-time 3700 \
  --request POST "$BASE/v1/audio-separation/jobs/wait" \
  --header "Authorization: Bearer $SERVICE_TOKEN" \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "source_uri": "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/BE2.6/test4.wav",
    "model": "bandit-v2-multilingual",
    "filename_prefix": "wait-demo"
  }'
```

等待超过服务器时限会返回504，并在 `detail` 中提供 `job_id` 和 `status_url`。任务不会被取消，调用方应继续使用查询接口获取结果。

## 7. 取消任务

### 中文接口名

取消音频分离任务

```http
POST /v1/audio-separation/jobs/{job_id}/cancel
```

```bash
curl --location --request POST \
  "$BASE/v1/audio-separation/jobs/$JOB_ID/cancel" \
  --header "Authorization: Bearer $SERVICE_TOKEN"
```

```json
{
  "job_id": "0ca30251-7006-4663-a27b-813dc07a0089",
  "status": "cancel_requested",
  "model": "bandit-v2-multilingual"
}
```

取消为尽力而为。排队任务通常可取消；已经进入GPU推理或上传阶段的任务可能仍然完成。

## 8. Python完整示例

```python
import os
import time
import requests

base = "https://aicentre2.sligenai.cn:8443"
headers = {"Authorization": f"Bearer {os.environ['SERVICE_TOKEN']}"}

created = requests.post(
    f"{base}/v1/audio-separation/jobs",
    headers=headers,
    json={
        "source_uri": "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/BE2.6/test4.wav",
        "model": "bandit-v2-multilingual",
        "filename_prefix": "python-demo",
        "external_ref": "order-python-001",
    },
    timeout=30,
)
created.raise_for_status()
job_id = created.json()["job_id"]

while True:
    response = requests.get(
        f"{base}/v1/audio-separation/jobs/{job_id}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if result["status"] == "succeeded":
        print(result["speech_url"])
        print(result["music_url"])
        print(result["sfx_url"])
        print(result["background_url"])
        break
    if result["status"] in {"failed", "cancelled"}:
        raise RuntimeError(result)
    time.sleep(2)
```

## 9. Node.js完整示例

Node.js 18及以上可直接使用 `fetch`：

```javascript
const base = "https://aicentre2.sligenai.cn:8443";
const headers = {
  Authorization: `Bearer ${process.env.SERVICE_TOKEN}`,
  "Content-Type": "application/json",
};

const createdResponse = await fetch(`${base}/v1/audio-separation/jobs`, {
  method: "POST",
  headers,
  body: JSON.stringify({
    source_uri: "https://bucket-silge-internal-products.oss-cn-shenzhen.aliyuncs.com/BE2.6/test4.wav",
    model: "bandit-v2-multilingual",
    filename_prefix: "node-demo",
    external_ref: "order-node-001",
  }),
});
if (!createdResponse.ok) throw new Error(await createdResponse.text());
const { job_id: jobId } = await createdResponse.json();

for (;;) {
  await new Promise((resolve) => setTimeout(resolve, 2000));
  const response = await fetch(`${base}/v1/audio-separation/jobs/${jobId}`, {
    headers: { Authorization: headers.Authorization },
  });
  if (!response.ok) throw new Error(await response.text());
  const result = await response.json();
  if (result.status === "succeeded") {
    console.log(result);
    break;
  }
  if (["failed", "cancelled"].includes(result.status)) {
    throw new Error(JSON.stringify(result));
  }
}
```

## 10. 状态码与重试建议

| HTTP状态码 | 含义 | 调用方处理 |
|---:|---|---|
| 200 | 查询、等待完成或取消请求成功 | 读取JSON。 |
| 202 | 异步任务已接收 | 保存 `job_id` 并轮询。 |
| 401 | Token缺失、格式错误或无效 | 检查 `Bearer ` 前缀和Token。 |
| 404 | 任务不存在或记录已过期 | 检查 `job_id`；任务查询记录默认保留7天。 |
| 413 | 素材超过512MiB | 缩小素材后重新提交。 |
| 415 | 格式不支持，或后缀/Content-Type/文件头冲突 | 修复对象存储类型或重新编码。 |
| 422 | URL、字段或字段类型无效 | 修正请求，不要原样重试。 |
| 502 | 下载、处理、推理或上传失败 | 检查源URL有效期；必要时退避重试。 |
| 503 | 队列或服务暂不可用 | 按2、5、10秒退避重试。 |
| 504 | 等待超时或源站下载超时 | 若响应含 `job_id`，继续查询；否则检查源站。 |

建议调用方保存自己的 `external_ref` 与返回的 `job_id`，轮询间隔使用2～5秒，避免高频查询。

## 11. 已验证性能

生产环境使用10.274833秒WAV测试：模型纯推理约2.04秒，RTF约0.199；包含下载、初始化和四轨OSS上传的端到端时间约8～11秒。该数据只用于容量估算，不是固定SLA；长素材的推理速度通常接近线性，排队和外部网络也会影响总耗时。
