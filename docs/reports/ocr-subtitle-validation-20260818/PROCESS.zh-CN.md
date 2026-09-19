# OCR与字幕测试流程

1. 用固定字体生成中文、英文、中英混合、数字、小字、加噪和旋转图片，并转换PNG/JPG/WebP。
2. 调用 `POST /v1/ocr/batch`，分别验证单图、ROI、6图和20图批量。
3. 并发1/2/4各执行10次干净中文图片，统计P50/P95/P99。
4. 生成6秒视频：0-2秒“第一段字幕”、2-4秒“第二段字幕”、4-6秒“第三段字幕”。
5. 经本机内部接口分别运行fast/balanced/accurate，检查事件、文字、QA、调试视频和审核HTML。
6. 验证无Token、SSRF、超量图片和runtime目录之外的字幕路径均被拒绝。
7. 保存CER、置信度、Worker、模型版本、耗时和逐秒资源采样。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_ocr_subtitle_module.py \
  --run-id ocr-subtitle-validation-20260818 \
  --output-dir runtime/validation/ocr-subtitle-validation-20260818 \
  --font runtime/validation/assets/msyh.ttc
```
