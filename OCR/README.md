# OCR Service

详细使用教程见：

```text
USAGE.md
```

本目录是视频翻译系统的本地生产化 OCR 服务。目标不是单次脚本识别，而是提供稳定的批量 OCR API，后续给字幕定位、bbox 调试视频和覆盖方案生成调用。

## 架构

```text
Client / video pipeline
  -> FastAPI OCR Gateway
  -> lazy OCR engine
  -> batch image / ROI recognition
  -> normalized JSON result
```

## 目录

```text
OCR/
  config/ocr.local.json
  scripts/install_cpu.ps1
  scripts/install_gpu.ps1
  scripts/run_api.ps1
  scripts/smoke_test.ps1
  src/ocr_service/
  tests/smoke_test.py
```

## 启动

```powershell
cd I:\AI-video\video-translate\OCR
.\scripts\run_api.ps1
```

默认地址：

```text
http://127.0.0.1:8096
```

健康检查：

```powershell
curl http://127.0.0.1:8096/health
```

## API

### POST /v1/ocr/batch

请求：

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
        {"name": "top", "bbox": [0, 0, 1080, 640]},
        {"name": "bottom", "bbox": [0, 640, 1080, 1920]}
      ]
    }
  ]
}
```

返回：

```json
{
  "job_id": "debug-001",
  "engine": "paddleocr",
  "model_version": "ppocrv6",
  "results": [
    {
      "image_id": "frame_0001",
      "time": 0.0,
      "items": [
        {
          "bbox": [10, 20, 200, 80],
          "text": "Las arrugas...",
          "score": 0.93,
          "region": "top"
        }
      ]
    }
  ]
}
```

## 生产约定

- OCR 结果必须记录 engine、model_version、device。
- 批量接口优先，避免逐帧逐请求。
- PaddleOCR 懒加载，启动服务不等于立刻占 GPU。
- PaddleOCR 不可用时，/health 会显示 engine unavailable，API 返回明确错误。
- 后续接入视频翻译时，只消费统一 JSON，不直接 import PaddleOCR。
