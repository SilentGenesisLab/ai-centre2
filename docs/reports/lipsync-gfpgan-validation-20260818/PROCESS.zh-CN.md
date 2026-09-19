# 唇形驱动与GFPGAN测试流程

1. 从已归档人物视频生成1/3/5秒样本，并从中文参考音频生成同长度WAV。
2. 分别关闭和开启GFPGAN提交URL任务，轮询至终态。
3. 并发2提交1秒任务，检查单并发队列行为和P95。
4. 下载OSS结果，检查容器、时长、分辨率和关键帧清晰度。
5. 验证任务列表、受限日志、取消、鉴权和SSRF拒绝。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_video_module.py \
  --module lipsync \
  --run-id lipsync-gfpgan-validation-20260818 \
  --output-dir runtime/validation/lipsync-gfpgan-validation-20260818
```
