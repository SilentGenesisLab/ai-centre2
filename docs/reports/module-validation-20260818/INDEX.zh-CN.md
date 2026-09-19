# AI Centre 其他模块生产测试总览

- 测试日期：2026-08-18
- 测试环境：`https://aicentre2.sligenai.cn:8443`
- 服务器：`/home/donxu/ai-centre`
- 原则：每个模块独立报告、独立测试流程、逐请求结果和逐秒资源采样；不执行GPU启停，不保存Token或OSS签名参数。

| 模块 | 结果 | 核心数据 | 独立报告 | 测试流程 |
| --- | --- | --- | --- | --- |
| ASR语音识别 | 通过 | 65/65 HTTP成功，平均CER 0.0095，并发4短音频P99 0.70秒 | [报告](../asr-validation-20260818-r2/REPORT.zh-CN.md) | [流程](../asr-validation-20260818-r2/PROCESS.zh-CN.md) |
| OCR文字识别 | 通过 | 41/41成功，测试集CER 0，P95 0.697秒 | [报告](../ocr-subtitle-validation-20260818/REPORT.zh-CN.md) | [流程](../ocr-subtitle-validation-20260818/PROCESS.zh-CN.md) |
| 字幕检测 | 阻断 | fast/balanced/accurate均503；127.0.0.1:8097未监听 | [报告](../ocr-subtitle-validation-20260818/REPORT.zh-CN.md) | [流程](../ocr-subtitle-validation-20260818/PROCESS.zh-CN.md) |
| 视频切片 | 通过 | 35/35完成，阈值10/27的五个硬切边界误差为0 | [报告](../scene-validation-20260818/REPORT.zh-CN.md) | [流程](../scene-validation-20260818/PROCESS.zh-CN.md) |
| 人脸处理 | 通过 | 34/34完成，有人脸25/25帧检出，无人脸0误检，音频保留 | [报告](../face-validation-20260818/REPORT.zh-CN.md) | [流程](../face-validation-20260818/PROCESS.zh-CN.md) |
| 唇形驱动/GFPGAN | 阻断 | 15/15在MuseTalk加载模型时CUDA OOM；GPU 0只剩12.94 MiB | [报告](../lipsync-gfpgan-validation-20260818/REPORT.zh-CN.md) | [流程](../lipsync-gfpgan-validation-20260818/PROCESS.zh-CN.md) |
| 水印处理 | 阻断 | 提交503；生产配置缺字段且专用Worker未安装 | [报告](../watermark-validation-20260818/REPORT.zh-CN.md) | [流程](../watermark-validation-20260818/PROCESS.zh-CN.md) |

## 建议修复顺序

1. 为MuseTalk预留足够的GPU 0显存或迁移冲突进程，然后原矩阵复测唇形与GFPGAN质量。
2. 部署字幕检测服务到`127.0.0.1:8097`并纳入健康检查，再运行三模式字幕边界测试。
3. 同步生产`control_plane/config.py`、安装水印专用Worker并完成light/intensive合规质量测试。
4. 观测层健康状态为`ok`且SQLite完整性检查通过，但本次高并发测试后进程内累计`dropped_events=9`、`last_error=null`，说明曾发生9次瞬时写入失败后恢复；建议补充异常类型持久化和写入重试，再核对调用记录与任务数。

测试通过不等于覆盖所有真实业务素材。当前矩阵优先验证接口契约、任务生命周期、格式、参数、并发、结果可用性和可量化质量；后续可再增加真实客户授权样本的盲测集。
