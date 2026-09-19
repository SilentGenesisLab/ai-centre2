# MiniMax H3 原生 768 档与视频超分测试记录

## 原生生成

- 输入：`input-first-10s.mp4`，前 10 秒
- 人物参考：`identity-reference.png`
- 模式：Ref2VA，4 步 Turbo，`res_multistep + simple`
- Seed：`582904731`
- 模型画布：`768×1344`
- 输出：`768×1344`、24 fps、10.125 秒、AAC 32 kHz 双声道
- 文件大小：3,181,408 bytes
- ComfyUI 执行耗时：861.077 秒，即 14 分 21 秒
- Worker：RTX 5090 32 GB

本次输出由 H3 直接生成 768×1344，不是从 480×864 插值放大。抽帧检查显示人物身份、正侧脸、浅蓝服装和科技展厅均保持稳定；背景小人物仍有生成式软化。FFmpeg `blurdetect` 全片均值为 9.752；该指标受内容边缘数量影响，只用于同源异常筛查，不能单独证明清晰度高低。

## 规格调整

AI Centre 的 `720p` 档改为 H3 原生 768 档：

- 9:16：768×1344
- 16:9：1344×768
- 1:1：768×768
- 4:3：1024×768
- 3:4：768×1024

后台和 API 默认清晰度同步改为 `720p`。480P继续保留为快速预览档。

## 超分接口盘点

`I:\Documents\默认模块.md` 中共有三条可测链路：

1. RunningHub FlashVSR，应用 ID `1996062530516795394`
2. RunningHub FlashVSR V2，应用 ID `1983119055743819777`
3. RunningHub SeedVR2，应用 ID `1990029249488801793`

当前文档未提供 RunningHub Base URL 和 Authorization 凭据，本机项目及环境变量也未配置 RunningHub API Key。因此本轮没有伪造超分结果，也没有把普通 FFmpeg 插值当作 AI 超分。取得授权配置后，三路均使用本次原生 768×1344 输出作为唯一输入，目标统一为约 1080×1920，并比较耗时、输出尺寸、文件大小、音画同步、面部纹理、手部、背景闪烁、过锐化与时序稳定性。

## 文件

- 原生结果：`runtime_validation/h3-10s-remix-20260904/output-ref2va-4step-768x1344.mp4`
- 抽帧总览：`runtime_validation/h3-10s-remix-20260904/contact-768x1344.jpg`
- ComfyUI记录：`runtime_validation/h3-10s-remix-20260904/history-ref2va-4step-768x1344.json`

