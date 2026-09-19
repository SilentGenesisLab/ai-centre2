# ASR 语音识别测试流程

1. 确认 `/health` 中 ASR 为 `ok`，且 `ai-centre-asr-gpu0.service` 正常运行。
2. 使用固定文案生成短、中、长中文WAV；转换为MP3、M4A、大写`.MP3`并封装一个MP4。
3. 复用11语种已归档参考音频，分别以 `language=auto` 和显式语言调用 `POST /v1/asr/transcriptions`。
4. 对中文短中长素材执行Beam 1/5/10测试；对格式样本验证文件头识别。
5. 并发1/2/4各执行10次短音频，统计P50/P95/P99。
6. 验证无Token、localhost/内网URL、非音视频内容和越界Beam均被拒绝。
7. 对返回正文计算CER/WER，检查segment起止时间有序且不倒退。
8. 保存逐请求结果和逐秒资源采样，不保存Service Token或OSS签名参数。

复现命令：

```bash
cd /home/donxu/ai-centre
.venv-control/bin/python scripts/validate_asr_module.py \
  --run-id asr-validation-20260818-r2 \
  --output-dir runtime/validation/asr-validation-20260818-r2 \
  --references runtime/validation/tts-retest-20260817/references.json
```
