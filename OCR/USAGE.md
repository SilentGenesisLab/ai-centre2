# 本地 PP-OCRv6 OCR 服务使用教程

本文档说明如何在本机使用已经部署好的 OCR 服务。当前服务用于视频翻译流程里的“字幕/标题/CTA 文字检测与识别”，输出统一 JSON，后续可接入字幕 bbox、覆盖方案、ASS 贴回等模块。

## 1. 当前部署信息

服务目录：

```powershell
I:\AI-video\video-translate\OCR
```

服务地址：

```text
http://127.0.0.1:8096
```

当前推荐运行环境：

```text
conda 环境：ocrpaddle
Python：D:\Anaconda3\envs\ocrpaddle\python.exe
OCR：PaddleOCR 3.7.0
模型：PP-OCRv6 medium det + rec
设备：gpu:0
GPU：本地 RTX 4090
```

注意：不要优先用 `tor25` 跑 OCR 服务。`tor25` 里有 PyTorch CUDA，和 Paddle GPU 的 CUDA/cuDNN DLL 容易冲突。当前已经单独建立了干净环境 `ocrpaddle`，更适合生产服务。

## 2. 常用命令

进入目录：

```powershell
cd I:\AI-video\video-translate\OCR
```

启动 GPU OCR 服务：

```powershell
.\scripts\start_api_ocrpaddle_gpu.ps1
```

查看状态：

```powershell
.\scripts\status_api.ps1
```

停止服务：

```powershell
.\scripts\stop_api.ps1
```

前台启动 GPU OCR 服务，适合调试：

```powershell
.\scripts\run_api_ocrpaddle_gpu.ps1
```

运行 GPU smoke test：

```powershell
.\scripts\smoke_test_ocrpaddle_gpu.ps1
```

如果只是想在 CPU 模式调试：

```powershell
.\scripts\run_api.ps1
```

但正式批量视频不建议用 CPU，速度会慢很多。

## 3. 健康检查

浏览器打开：

```text
http://127.0.0.1:8096/health
```

PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8096/health | ConvertTo-Json -Depth 8
```

正常结果大概如下：

```json
{
  "status": "ok",
  "config": {
    "engine": "paddleocr",
    "model_version": "ppocrv6",
    "device": "gpu:0"
  },
  "engine": {
    "loaded": true,
    "load_error": null
  }
}
```

字段说明：

- `status=ok`：API 服务正常。
- `device=gpu:0`：当前使用第 0 张 GPU。
- `loaded=true`：PaddleOCR 模型已经加载。
- `load_error=null`：没有模型加载错误。

如果 `loaded=false`，不一定是错误。服务是懒加载，第一次调用 OCR 接口时才会真正加载模型。

## 4. OCR 接口

接口地址：

```http
POST http://127.0.0.1:8096/v1/ocr/batch
```

请求格式：

```json
{
  "job_id": "debug-001",
  "source_lang_hint": "es",
  "images": [
    {
      "image_id": "frame_0001",
      "path": "I:/path/to/frame.jpg",
      "time": 0.0,
      "regions": [
        {
          "name": "top",
          "bbox": [0, 0, 1080, 640]
        },
        {
          "name": "bottom",
          "bbox": [0, 640, 1080, 1920]
        }
      ]
    }
  ]
}
```

参数说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `job_id` | 是 | 本次 OCR 任务 ID，方便日志追踪 |
| `source_lang_hint` | 否 | 源语言提示，例如 `es`、`pt`、`th`、`zh`、`en` |
| `images` | 是 | 图片列表，建议批量提交 |
| `image_id` | 是 | 图片 ID，例如帧号 |
| `path` | 是 | 本地图片绝对路径 |
| `time` | 否 | 该帧在视频里的时间，单位秒 |
| `regions` | 否 | ROI 区域列表，不传则识别整张图 |
| `bbox` | 否 | `[x1, y1, x2, y2]`，像素坐标 |

返回格式：

```json
{
  "job_id": "debug-001",
  "engine": "paddleocr",
  "model_version": "ppocrv6",
  "device": "gpu:0",
  "elapsed_seconds": 0.23,
  "results": [
    {
      "image_id": "frame_0001",
      "time": 0.0,
      "items": [
        {
          "bbox": [100, 120, 980, 240],
          "text": "Las arrugas son pequeños problemas",
          "score": 0.94,
          "region": "top"
        }
      ]
    }
  ]
}
```

返回字段说明：

| 字段 | 说明 |
|---|---|
| `engine` | OCR 引擎，目前是 `paddleocr` |
| `model_version` | 模型版本，目前是 `ppocrv6` |
| `device` | 推理设备，例如 `gpu:0` |
| `elapsed_seconds` | 本次请求耗时 |
| `bbox` | OCR 检出的文字矩形框 |
| `text` | 识别出的文字 |
| `score` | 识别置信度 |
| `region` | 来源 ROI 名称，例如 `top`、`bottom` |

## 5. PowerShell 调用示例

先准备一张图片，比如：

```text
I:\test\frame001.jpg
```

调用：

```powershell
$body = @{
  job_id = "test-001"
  source_lang_hint = "es"
  images = @(
    @{
      image_id = "frame001"
      path = "I:/test/frame001.jpg"
      time = 0.0
      regions = @(
        @{
          name = "full"
          bbox = @(0, 0, 1080, 1920)
        }
      )
    }
  )
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8096/v1/ocr/batch" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body |
  ConvertTo-Json -Depth 10
```

## 6. Python 调用示例

```python
import requests

payload = {
    "job_id": "video-001",
    "source_lang_hint": "es",
    "images": [
        {
            "image_id": "frame_000120",
            "path": "I:/frames/frame_000120.jpg",
            "time": 4.0,
            "regions": [
                {"name": "top", "bbox": [0, 0, 1080, 640]},
                {"name": "bottom", "bbox": [0, 640, 1080, 1920]},
            ],
        }
    ],
}

response = requests.post(
    "http://127.0.0.1:8096/v1/ocr/batch",
    json=payload,
    timeout=120,
)
response.raise_for_status()
print(response.json())
```

## 7. 批量视频里的推荐用法

视频字幕识别不要一帧一请求。推荐这样：

```text
视频抽帧
→ 按分镜/时间采样
→ 每帧裁 top/bottom ROI
→ 组 batch 调 OCR
→ 得到 bbox/text/score
→ 分镜内合并稳定字幕事件
→ 输出 subtitle_events.json
```

建议批量大小：

```text
images per request：16～64 起步
每张图 regions：top/bottom 两个 ROI
max_images_per_request：当前配置 128
max_regions_per_image：当前配置 8
```

不建议：

```text
每一帧单独 POST 一次
整段视频直接丢给 OCR
把 OCR 结果和覆盖逻辑写死在同一个脚本里
```

## 8. 和视频翻译流程的关系

OCR 服务只负责：

```text
图片/ROI → bbox + text + score
```

它不负责：

```text
字幕时间轴合并
翻译
覆盖色块生成
ASS 字幕排版
TTS 配音
视频合成
```

推荐下游输出结构：

```json
{
  "events": [
    {
      "track": "top",
      "start": 0.0,
      "end": 3.2,
      "bbox": [80, 120, 960, 260],
      "source_text": "Las arrugas son pequeños problemas",
      "source_lang": "es",
      "ocr_engine": "paddleocr",
      "model_version": "ppocrv6",
      "score": 0.94,
      "should_cover": true,
      "should_translate": true
    }
  ]
}
```

## 9. 配置文件

GPU 配置：

```text
I:\AI-video\video-translate\OCR\config\ocr.tor25.gpu.json
```

虽然文件名里有 `tor25`，当前推荐实际是用 `ocrpaddle` 环境运行。后续可以重命名成 `ocr.gpu.json`。

主要配置：

```json
{
  "host": "127.0.0.1",
  "port": 8096,
  "engine": "paddleocr",
  "model_version": "ppocrv6",
  "device": "gpu:0",
  "text_detection_model_dir": "C:/Users/Admin/.paddlex/official_models/PP-OCRv6_medium_det",
  "text_recognition_model_dir": "C:/Users/Admin/.paddlex/official_models/PP-OCRv6_medium_rec",
  "max_images_per_request": 128,
  "max_regions_per_image": 8
}
```

如果要切 CPU：

```json
"device": "cpu"
```

如果要换 GPU：

```json
"device": "gpu:1"
```

## 10. 日志与进程

PID 文件：

```text
I:\AI-video\video-translate\OCR\runtime\ocr-api.pid
```

日志：

```text
I:\AI-video\video-translate\OCR\runtime\logs\ocr-api.out.log
I:\AI-video\video-translate\OCR\runtime\logs\ocr-api.err.log
```

查看日志：

```powershell
Get-Content .\runtime\logs\ocr-api.out.log -Tail 100
Get-Content .\runtime\logs\ocr-api.err.log -Tail 100
```

## 11. 常见问题

### 11.1 health 正常，但 loaded=false

这是正常的。OCR 模型是懒加载，第一次请求 `/v1/ocr/batch` 时才加载。

### 11.2 端口被占用

先停止旧服务：

```powershell
.\scripts\stop_api.ps1
```

如果还不行，检查端口：

```powershell
netstat -ano | findstr 8096
```

### 11.3 tor25 里为什么 GPU 跑不起来

`tor25` 有 PyTorch CUDA，Paddle GPU 也有 CUDA/cuDNN。Windows 下同名 DLL 容易互相抢加载路径，导致 PaddleOCR 误加载 PyTorch 的 cuDNN DLL。

所以生产建议：

```text
OCR 独立环境：ocrpaddle
PyTorch/TTS/ASR 环境：tor25 或其它环境
二者通过 HTTP API 通信
```

### 11.4 出现 CUDNN 版本 warning

日志可能出现：

```text
Paddle compiled with CUDNN 9.9, but CUDNN version in your machine is 9.5
```

当前 smoke test 已通过。如果高并发压测出现异常，再统一 Paddle 与 cuDNN 版本。短期可以继续使用。

### 11.5 中文/泰语/西语/葡语都能识别吗

当前 PP-OCRv6 更适合：

```text
中文、英文、西班牙语、葡萄牙语、其它拉丁语系
```

泰语建议后续补：

```text
PP-OCRv5 multilingual / Thai 兜底
```

工程上建议做 OCR 路由：

```text
拉丁语系/中文/英文 → PP-OCRv6
泰语/低置信度 → PP-OCRv5 multilingual 或其它兜底模型
```

## 12. 下一步建议

为了接入视频翻译主流程，建议继续开发三个模块：

1. `extract_video_frames.py`  
   抽帧并生成 top/bottom ROI。

2. `detect_subtitle_events.py`  
   调 OCR 服务，合并同一分镜内的 bbox，输出 `subtitle_events.json`。

3. `export_bbox_debug_video.py`  
   把 OCR bbox 画成白色/彩色 mask 视频，方便人工 QA。

最终流程：

```text
视频
→ 抽帧/ROI
→ OCR API
→ subtitle_events.json
→ bbox debug video
→ 覆盖方案
→ 翻译/ASS/TTS/合成
```

