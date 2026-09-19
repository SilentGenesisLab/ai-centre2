# 视频调色异步接口

`POST /v1/color-grade/jobs` 接收公网 HTTPS 视频和 `.cube` LUT 地址。任务使用独立的 Celery `color_grade` 队列运行，完成后把 MP4 上传到中台配置的 `KERNEL_UPLOAD_URL`，并在任务结果中返回 `video_url`。

```json
{
  "video_url": "https://storage.example.com/video/source.mp4",
  "cube_url": "https://storage.example.com/luts/warm.cube",
  "strength": 0.65,
  "filename": "warm_graded.mp4"
}
```

`strength` 范围是 `0` 到 `1`：`0` 保留原片，`1` 使用完整 LUT，默认 `0.65`。处理过程中会保留原音频；LUT 会先清理注释和标题，只保留标准 17³、33³ 或 65³ 3D 表，避免 FFmpeg 对非标准 `.cube` 头部解析失败。

提交任务后使用返回的 `status_url` 查询状态。成功结果包含 `video_url`、`strength` 和 `lut_size`。
