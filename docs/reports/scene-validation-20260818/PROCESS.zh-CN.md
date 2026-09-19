# 视频切片测试流程

1. 生成5秒、每秒一次硬切换的五色受控视频。
2. 测试threshold 10/27/45和min_scene_len 15/30。
3. 覆盖等待接口、异步轮询和并发1/2/4各10次。
4. 检查场景数、起止时间、切片OSS URL和首个切片容器。
5. 验证取消、鉴权和SSRF拒绝。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_video_module.py \
  --module scene \
  --run-id scene-validation-20260818 \
  --output-dir runtime/validation/scene-validation-20260818
```
