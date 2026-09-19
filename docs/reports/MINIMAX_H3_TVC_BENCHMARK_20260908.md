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

统一HTML审核页已改为50条成片页面内直接播放，并保留10%、50%、90%抽帧作为快速定位依据。主矩阵36条、质量档位A/B 6条、早期时长与参考图A/B 8条已合并展示。

## 最终人工商业审核结论

### 总体判断

- 接口稳定性通过：主矩阵36/36成功，36/36完整解码，提交与生产链路没有出现技术失败。
- 内容质量不能按100%成功计算。抽帧和成片复核发现产品身份漂移、伪文字、参考源语义污染和长时长结构漂移；这些属于内容失败或需返修，不应被HTTP成功率掩盖。
- H3已经适合批量生产创意预览、气氛镜头和无精确文字要求的产品辅助镜头，但尚不能只靠纯文本稳定生成指定SKU的最终英雄镜头。
- 当前最可靠生产方式是：干净SKU参考图/参考视频 + 5～10秒单一镜头 + 后期合成真实Logo与包装文字。

### 分辨率、时长和输入方式

| 维度 | 结论 |
|---|---|
| 480P | 速度最快，适合内部预览；细节和暗部层次有限，不建议直接交付。 |
| 720P | 当前常规生产性价比最佳；画质相较480P明显改善，耗时显著低于1080P长视频。 |
| 1080P | 金属、玻璃和水滴细节最好，适合关键镜头；平均有效耗时427.884秒，P95为991.658秒。 |
| 5秒 | 适合单一动作和英雄定格，平均132.887秒；叙事空间有限。 |
| 10秒 | 综合稳定性最佳，适合单镜头广告素材，平均318.891秒。 |
| 15秒 | 平均546.515秒，P95为1031.754秒；更容易发生产品形态和场景漂移，建议拆成多个5秒镜头。 |
| 纯文本 | 平均178.162秒，只能稳定生成“某类产品”，不能保证指定SKU。 |
| 参考视频 | 平均487.366秒，约为纯文本的2.74倍；能增强构图和运动继承，但会把参考源中的错误场景一起带入。 |

### 关键质量问题

1. 智能手表纯文本结果在圆形机械表、圆形智能表和与方形智能表之间变化，说明“产品类别”基本正确，但SKU身份不稳定。
2. 智能手表参考视频为方形表，而提示词要求圆形表壳；结果多数继承方形参考源，说明参考素材的控制权高于冲突文字描述。
3. 精华参考视频并非干净产品素材：开头是瓶体，中段和结尾出现无关男性人物。720P 5秒和10秒结果直接继承人物镜头。这是参考源污染，不应误判为模型随机生成无关人物。
4. 精华瓶身在各分辨率中普遍出现伪品牌、乱码或无法识别的包装文字；Logo和包装文案必须后期合成。
5. 15秒样本的构图完成度并未稳定优于10秒，耗时和漂移风险却明显增加，因此单段15秒不是推荐默认值。

### 质量档位补充结论

- low：两场景平均有效处理263.232秒，适合快速预览和选镜。
- medium：平均517.181秒，建议作为常规生产默认档。
- high：平均506.033秒，材质、边缘和粒子略好于medium，但对产品身份漂移改善有限，只建议关键英雄镜头使用。
- high本轮略快于medium属于单样本波动，不能解释为稳定性能优势。

## 推荐生产配置

- 默认：720P、5～10秒、medium、单段、提供干净SKU参考图或只包含目标产品的参考视频。
- 预览：low；英雄镜头：high；1080P只用于最终入选镜头。
- 参考视频提交前增加内容门禁：抽取10%、50%、90%帧，检查是否包含无关人物、场景切换和错误产品。
- 产品文字、Logo和商标统一后期叠加，不让生成模型负责准确排版。
- 业务质量统计应同时展示“技术成功率”和“内容可用率”；本轮只能确认前者为100%，内容可用率需在统一审核页完成逐条人工标注后再形成正式SLA。
