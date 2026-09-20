# MiniMax H3 low / medium / high 质量档位A/B报告

本次使用同Prompt、同Seed、720P、10秒、16:9，分别验证纯文本TVC与参考视频TVC。

成功率：6/6；完整解码率：1.0

| 场景 | 质量 | 有效处理(s) | 排队(s) | 端到端(s) | 尺寸 | 黑帧 | 冻结 | 成片 |
|---|---|---:|---:|---:|---|---:|---:|---|
| 智能手表·纯文本 | low | 128.301 | 0.044 | 128.345 | 1344×768 | 4 | 0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/d174e0e9321f49f1a23d6332a2d19ff0.mp4) |
| 智能手表·纯文本 | medium | 303.435 | 63.743 | 367.177 | 1344×768 | 3 | 0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/46b03c42bb814d108335935f0429e9cf.mp4) |
| 智能手表·纯文本 | high | 304.999 | 126.303 | 431.302 | 1344×768 | 0 | 0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/fc8e096152ac4401bd44dc747269a247.mp4) |
| 护肤精华·参考视频 | low | 398.163 | 0.493 | 398.656 | 1344×768 | 0 | 0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/b3fc4f73c75f4909a5e2e457ef6ab32b.mp4) |
| 护肤精华·参考视频 | medium | 730.927 | 0.495 | 731.422 | 1344×768 | 0 | 0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/7a8441ce3e6841a2b6003779fc11ce37.mp4) |
| 护肤精华·参考视频 | high | 707.067 | 0.519 | 707.585 | 1344×768 | 0 | 0 | [OSS](https://oss-imgai.sligenai.cn/ai-video-kernel/20260908/1a57ae8656d04891bf926e9fc85906a4.mp4) |

## 档位实现

- low：较小内部画布，Euler 4步，最终交付仍为720P。
- medium：原生720P级内部画布，Euler 4步。
- high：与medium相同内部画布，改用res_multistep质量采样器。

主观画质、产品稳定性和参考继承结论以配套三帧审核页与完整成片盲审为准。
