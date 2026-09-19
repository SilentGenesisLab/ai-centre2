# MuseTalk + GFPGAN 生产部署

## 生产位置

- 公网入口：`https://aicentre2.sligenai.cn:8443`
- 中台：`127.0.0.1:8320`
- MuseTalk 内部服务：`127.0.0.1:9011`
- 项目目录：`/home/donxu/ai-centre`
- MuseTalk：`/home/donxu/services/MuseTalk`
- GFPGAN 权重：`/home/donxu/services/GFPGAN/GFPGANv1.3.pth`
- 任务数据：`/home/donxu/ai-centre/runtime/musetalk/jobs`
- systemd：`ai-centre-musetalk.service`

内部服务只监听本机。公网请求必须经过 Caddy 和中台 Bearer 鉴权。

## API

提交任务：

```bash
curl -X POST 'https://aicentre2.sligenai.cn:8443/v1/lipsync/jobs' \
  -H 'Authorization: Bearer <SERVICE_TOKEN>' \
  -F 'video=@face.mp4' \
  -F 'audio=@speech.wav' \
  -F 'face_restore=true'
```

`face_restore` 默认是 `false`。GFPGAN 对真人通常能提高清晰度，但对动漫脸或
已经很清晰的人脸可能产生身份漂移，所以生产调用应按素材显式开启。

```text
GET  /v1/lipsync/jobs?limit=50&state=completed
GET  /v1/lipsync/jobs/{job_id}
GET  /v1/lipsync/jobs/{job_id}/video
GET  /v1/lipsync/jobs/{job_id}/logs?stage=musetalk&tail=200
POST /v1/lipsync/jobs/{job_id}/cancel
```

任务阶段为 `queued → musetalk → gfpgan → uploading → completed`。关闭人脸修复时会跳过
`gfpgan`。生产环境启用 OSS 后，`completed` 状态的 `result_url` 为可直接访问的
OSS HTTPS 地址；中台鉴权下载接口继续作为兜底。任务状态和日志均保存在任务私有目录中。

接口返回 `202 Accepted` 后，任务会先原子写入磁盘再自动进入单任务队列，无需人工执行命令。
服务重启后，磁盘上的 `queued` 任务会按创建时间继续执行；中断在 `musetalk` 或
`gfpgan` 阶段的任务会从 MuseTalk 阶段安全重跑，原有日志按 `recovery-N` 保留。
已完成、失败或取消的任务不会重复执行。

## 运行约束

- 固定 GPU0，单任务并发 1。
- MuseTalk 和 GFPGAN 在不同子进程中顺序运行，阶段结束后释放显存。
- 每个任务默认超时 1800 秒，取消或超时会终止完整进程组。
- 只接受常见视频和音频扩展名，不接受服务器文件路径。
- 单文件默认最大 512MiB。

## 运维

```bash
systemctl --user status ai-centre-musetalk.service
journalctl --user -u ai-centre-musetalk.service -f
curl http://127.0.0.1:9011/health
```

用户服务已启用 systemd lingering，服务器重启后即使没有 SSH 登录也会自动启动。

重复部署：

```bash
cd /home/donxu/ai-centre
./scripts/download_musetalk_models.sh
./scripts/install_musetalk.sh
```

## 2026-08-01 验收结果

| 样片 | 输入 | MuseTalk | GFPGAN | 总耗时 | 结果 |
|---|---:|---:|---:|---:|---|
| 动漫人脸 | 3秒 / 75帧 | 34.5秒 | 12.6秒 | 47.1秒 | 口型变化正常；GFPGAN 身份漂移明显 |
| 真人人脸 | 3秒 / 75帧 | 23.1秒 | 24.2秒 | 47.4秒 | 口型变化和脸部稳定；GFPGAN 清晰度提升 |

两次输出均保持 3.00 秒、25fps、H.264 视频和 16kHz 单声道 AAC 音轨。观测到的
GPU0 最高占用约 24.7GiB。公网状态查询和视频下载均返回 HTTP 200，服务器全量
单元测试 26 项通过。
