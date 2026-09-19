# MiniMax H3 10秒视频迁移调研与实验（内部源稿）

面向AI Centre研发与运营，2026-09-04。范围为本地开源MiniMax H3在RTX 5090上的10秒人物、环境、背景迁移，不评估官方闭源Context-IR或Regenerate-2K API。

直接结论：视频迁移必须使用Ref2VA，并使用参考图锁定替换人物身份；只靠文本会被源视频身份条件压制。4步480×864实测约8.5分钟、峰值23.28GiB。U17的双采和latent放大适合终稿后处理，但其FL2VA主链不应直接用于视频编辑。

证据账本：MiniMax官方模型卡支持模式、输入上限、时长/帧率/音频规格；ComfyUI官方文档支持本地节点；AIMixer README支持任务族、默认采样和二采设计；LightX2V模型卡限定公开8步质量声明主要针对FL2V。实验原始记录位于runtime_validation/h3-10s-remix-20260904。
