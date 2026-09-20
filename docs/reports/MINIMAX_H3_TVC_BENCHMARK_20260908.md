# MiniMax H3 TVC 广告全矩阵测评报告

- 测试时间：2026-09-08T08:11:00.044996+00:00 至 2026-09-08T11:07:49.047491+00:00
- 正式样本：36 条（2类创意 × 3分辨率 × 3时长 × 2输入模式）
- 成功：36/36，成功率：1.0
- 完整解码通过率：1.0
- 所有任务优先级为1；运行时全部H3活跃任务总数限制为3。

## 耗时总览

主耗时口径为最终成功 Attempt 的有效处理时间，不包含排队等待；端到端耗时单独保留。

| 维度 | 分组 | 数量 | 有效均值(s) | P50(s) | P95(s) | 端到端均值(s) |
|---|---|---:|---:|---:|---:|---:|
| 分辨率 | 480p | 12 | 202.653 | 157.249 | 464.492 | 209.492 |
| 分辨率 | 720p | 12 | 367.756 | 301.851 | 871.103 | 371.183 |
| 分辨率 | 1080p | 12 | 427.884 | 350.449 | 991.658 | 433.817 |
| 时长 | 5 | 12 | 132.887 | 98.365 | 255.776 | 136.538 |
| 时长 | 10 | 12 | 318.891 | 261.524 | 669.538 | 327.946 |
| 时长 | 15 | 12 | 546.515 | 436.077 | 1031.754 | 550.008 |
| 输入 | text | 18 | 178.162 | 109.769 | 434.067 | 178.183 |
| 输入 | video | 18 | 487.366 | 443.178 | 965.899 | 498.145 |
| 创意 | smart_watch | 18 | 288.325 | 221.283 | 826.421 | 293.077 |
| 创意 | blue_serum | 18 | 377.204 | 256.86 | 965.899 | 383.251 |

## 逐条结果

| 案例 | 输入 | 分辨率 | 目标时长 | 状态 | 有效处理(s) | 端到端(s) | 技术检测 | 成片 |
|---|---|---|---:|---|---:|---:|---|---|
| 高端智能手表 | text | 480p | 5 | succeeded | 33.546 | 33.568 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/4b12c0f4c1354df59dc72bf7deffaff6.mp4) |
| 高端智能手表 | video | 480p | 5 | succeeded | 100.685 | 101.159 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/e7a52aa749c349e0a3e101b2f182dddd.mp4) |
| 浅蓝科技护肤精华 | text | 480p | 5 | succeeded | 38.086 | 38.104 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/0eb4b30dd1624125836b35f328da45c2.mp4) |
| 浅蓝科技护肤精华 | video | 480p | 5 | succeeded | 192.225 | 192.447 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/3fa4760c6c3341e2a55a26c2c1423d84.mp4) |
| 高端智能手表 | text | 720p | 10 | succeeded | 40.259 | 40.28 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/c1c1fbd7b93c4a4e882055061db97ef6.mp4) |
| 高端智能手表 | video | 720p | 10 | succeeded | 439.32 | 439.524 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/7d13d539d7e440538f791001791e5d49.mp4) |
| 浅蓝科技护肤精华 | text | 720p | 10 | succeeded | 183.799 | 183.821 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/a3ee5977477048b4bea52eaccdaaf5cb.mp4) |
| 浅蓝科技护肤精华 | video | 720p | 10 | succeeded | 633.488 | 633.805 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/24ff12c686a646e2a09ddab925235162.mp4) |
| 高端智能手表 | text | 1080p | 15 | succeeded | 433.206 | 433.229 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/38c506a6586b468299cbecc713a2b249.mp4) |
| 高端智能手表 | video | 1080p | 15 | succeeded | 860.071 | 860.242 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/7fc884ee932a4fa18dd801845019fc20.mp4) |
| 浅蓝科技护肤精华 | text | 1080p | 15 | succeeded | 438.947 | 438.964 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/cc9d06211e4c4a3a9a5ab6cf07a569b3.mp4) |
| 浅蓝科技护肤精华 | video | 1080p | 15 | succeeded | 1152.487 | 1153.009 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/2ec8dc41b5294d2da60273092b999ff9.mp4) |
| 高端智能手表 | text | 1080p | 5 | succeeded | 96.045 | 96.062 | 解码=True,黑段=2,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/657ca4f910d54b0ca2210563a6b93169.mp4) |
| 高端智能手表 | video | 1080p | 5 | succeeded | 180.2 | 180.441 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/bfa5f84960954bedb82aaa42c0caa48b.mp4) |
| 浅蓝科技护肤精华 | text | 1080p | 5 | succeeded | 86.332 | 86.349 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/d1d91c5ebdce4b8c81a6cfe2f3c52b62.mp4) |
| 浅蓝科技护肤精华 | video | 1080p | 5 | succeeded | 267.692 | 309.973 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/70a638cbe37c49268ad09a0e34811f06.mp4) |
| 高端智能手表 | text | 480p | 10 | succeeded | 97.265 | 97.285 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/32de67d2e3d34d8689a2be4d382ed16e.mp4) |
| 高端智能手表 | video | 480p | 10 | succeeded | 289.711 | 332.703 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/6a45f1ab2e7f43d9851231177240d435.mp4) |
| 浅蓝科技护肤精华 | text | 480p | 10 | succeeded | 75.805 | 75.837 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/50190e768dac4357b3a1bfb3f07410e2.mp4) |
| 浅蓝科技护肤精华 | video | 480p | 10 | succeeded | 447.414 | 484.563 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/6a3018f0ac91435590ceb0c4d86c6754.mp4) |
| 高端智能手表 | text | 720p | 15 | succeeded | 357.675 | 357.698 | 解码=True,黑段=0,冻结=1 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/c9817f55d5cb40dbbef9a922f81e2702.mp4) |
| 高端智能手表 | video | 720p | 15 | succeeded | 820.483 | 859.958 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/4d9058b8e08b46ba936a812bc8aaebc9.mp4) |
| 浅蓝科技护肤精华 | text | 720p | 15 | succeeded | 405.24 | 405.263 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/f97d471ae37c4242a854e4c2a6bb6cc1.mp4) |
| 浅蓝科技护肤精华 | video | 720p | 15 | succeeded | 932.972 | 933.489 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/25ae31fb6dc24b33be2e2aec13beb0e6.mp4) |
| 高端智能手表 | text | 720p | 5 | succeeded | 74.922 | 74.943 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/3cc387504b024781a85e9915c4a293a6.mp4) |
| 高端智能手表 | video | 720p | 5 | succeeded | 209.228 | 209.438 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/8734a1c2f617464e8dc0335035ebdcf2.mp4) |
| 浅蓝科技护肤精华 | text | 720p | 5 | succeeded | 69.655 | 69.674 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/bf829241a7294d5088b2febe90e46f34.mp4) |
| 浅蓝科技护肤精华 | video | 720p | 5 | succeeded | 246.027 | 246.299 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/fb89fc892f9343508ab2d713658af9c2.mp4) |
| 高端智能手表 | text | 1080p | 10 | succeeded | 233.338 | 233.359 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/bc4e6203e40a4f17bb49470d65ebca87.mp4) |
| 高端智能手表 | video | 1080p | 10 | succeeded | 447.035 | 448.028 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/c2bfb77e8f0a42afaabd23a17494b646.mp4) |
| 浅蓝科技护肤精华 | text | 1080p | 10 | succeeded | 225.66 | 225.676 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/75a02ce94c324502acc56d7d33a31297.mp4) |
| 浅蓝科技护肤精华 | video | 1080p | 10 | succeeded | 713.599 | 740.476 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/a63d5cf563cb4740b475ee401a30be1d.mp4) |
| 高端智能手表 | text | 480p | 15 | succeeded | 122.273 | 122.291 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/be754a4c96614f949fbaf9a5e3282ff8.mp4) |
| 高端智能手表 | video | 480p | 15 | succeeded | 354.588 | 355.183 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/1fea6f5330724a73ab6234d4b976c45d.mp4) |
| 浅蓝科技护肤精华 | text | 480p | 15 | succeeded | 194.871 | 194.894 | 解码=True,黑段=0,冻结=0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/1534e0e319d94222b59c38b3886fc640.mp4) |
| 浅蓝科技护肤精华 | video | 480p | 15 | succeeded | 485.365 | 485.87 | 解码=True,黑段=0,冻结=1 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/a7959cd6e43444c1b6945b029a4a45ea.mp4) |

## 效果审核说明

HTML审核页为每条成片抽取10%、50%、90%三帧并提供OSS播放链接。重点人工检查广告主题符合度、产品身份稳定、参考视频镜头继承、商品形变、乱码/伪Logo、镜头稳定、动作自然以及直接剪辑可用性。最终主观结论将在人工审核后补入本报告。
