# 水印处理（合规运行验证）测试流程

1. 生成带“AUTHORIZED TEST MATERIAL”可见标签的自有3秒视频。
2. 检查专用Worker是否运行。
3. Worker可用时测试light/intensive并检查结果容器；不可用时只提交异步任务、确认队列状态并取消。
4. 验证鉴权和SSRF拒绝。
5. 不测试隐藏水印提取规避或平台溯源对抗。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_video_module.py \
  --module watermark \
  --run-id watermark-validation-20260818 \
  --output-dir runtime/validation/watermark-validation-20260818
```
