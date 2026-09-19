# 人脸处理测试流程

1. 生成1/3/5秒有人脸视频和3秒无人脸视频。
2. 覆盖等待接口和异步提交/轮询接口。
3. 并发1/2/4各执行10次1秒样本。
4. 检查applied、处理帧、有人脸帧、检测框、执行Provider和OSS结果。
5. 验证无人脸不误处理、取消、鉴权和SSRF拒绝。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_video_module.py \
  --module face \
  --run-id face-validation-20260818 \
  --output-dir runtime/validation/face-validation-20260818
```
