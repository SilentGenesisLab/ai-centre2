# AI Centre 2 音频分离生产部署

## 生产能力

AI Centre 2 使用 Bandit v2 DnR v3 Multilingual，将公网 HTTPS 音频或视频分离为四个 48 kHz、双声道、PCM16 WAV 结果：

- `speech_url`：影视对白。歌唱通常归入音乐轨，不应当作歌曲人声提取接口。
- `music_url`：音乐。
- `sfx_url`：音效。
- `background_url`：音乐与音效的合成背景轨。

结果上传 OSS，接口只返回 HTTPS URL。输入下载受 SSRF 校验、文件头校验、512 MiB 上限和超时保护约束。

## API

```text
POST /v1/audio-separation/jobs
POST /v1/audio-separation/jobs/wait
GET  /v1/audio-separation/jobs/{job_id}
POST /v1/audio-separation/jobs/{job_id}/cancel
```

公开请求示例见 [EXTERNAL_API.md](EXTERNAL_API.md) 和线上 Swagger。异步提交进入普通队列；`wait` 使用较高优先级并保持连接等待结果。

## 生产布局

```text
项目目录     /home/donxu/ai-centre
模型目录     /home/donxu/services/audio-separation-bandit
任务目录     /home/donxu/ai-centre/runtime/audio-separation
Celery 队列  audio_separation
systemd      ai-centre-audio-separation-worker.service
物理 GPU     GPU 1
并发         1
```

Worker 使用控制面虚拟环境接收 Celery 任务，再通过 `.venv-tts-v026` 的独立子进程执行推理。子进程结束后释放 Bandit CUDA 缓存。音频分离与视频深度共用：

```text
/home/donxu/ai-centre/runtime/video-depth/gpu.lock
```

该锁避免两个高显存离线任务同时进入 GPU。任务工作目录无论成功或失败都会清理。

## 固定版本与许可

```text
上游 commit  d5563d9031e95fdaa3e5a73d5020b9a0df61adb6
权重文件     checkpoint-multi.slim.pt
SHA-256      ba9ba16504cd5d987a8c01a00307afba1340f251c18c70406c787bf76e2c4102
```

Bandit v2 代码为 Apache-2.0；本次多语言权重发布许可为 CC BY-SA 4.0。正式对外商用前需落实权重署名与相同方式共享义务。不要将旧 Bandit Plus 的非商业权重替换到生产服务。

## 运维检查

```bash
systemctl --user status ai-centre-audio-separation-worker.service
journalctl --user -u ai-centre-audio-separation-worker.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8320/health
nvidia-smi
```

观测服务名为 `separation`，计量单位为 `audio_minute`。价格未配置时只记录用量，账目状态为 `unpriced`，金额为 0。

## 生产验收记录

2026-08-20 使用 10.274833 秒 WAV 完成公网异步闭环：

```text
任务 ID       0ca30251-7006-4663-a27b-813dc07a0089
最终状态      succeeded
推理耗时      2.0398 秒
RTF           0.198524
结果          四个 HTTPS OSS URL 均生成成功
观测任务      separation / separate
用量          0.1712472167 audio_minute
```

完整的多语言、真实视频和 MDX A/B 报告位于 [video-audio-separation-20260820](reports/video-audio-separation-20260820/REPORT.zh-CN.md)。

## 回滚

部署前完整备份：

```text
/home/donxu/ai-centre/runtime/deploy-backups/audio-separation-pre-20260820-1717.tgz
```

回滚时先停止并禁用新增 Worker，再恢复备份中的控制面、后台和 systemd 文件，执行 `systemctl --user daemon-reload`，最后仅重启控制面与后台。不要重启 ASR、TTS、OCR、Face 或 MuseTalk。
