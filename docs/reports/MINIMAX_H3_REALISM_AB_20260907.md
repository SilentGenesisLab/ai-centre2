# MiniMax H3 去油腻与真实感优化 A/B 测试

日期：2026-09-07  
范围：同一10秒源视频、同一人物身份、Ref2VA、768×1344、24fps、Seed 582904731

## 结论

“油腻感”由三个因素叠加：参考图本身的棚拍高光与精修肤质、泛化营销Prompt，以及4步Turbo的质感偏置。仅写“matte skin / avoid glossy skin”不能稳定消除油光；参考图光源形态和人物表情会显著改变H3的构图与皮肤表现。

本次综合最佳仍是第三版“漫射光哑光参考 + 写实Ref2VA Prompt”。它的办公室、背景人物、衣料和整体摄影感明显优于原商业版，人物近景不再持续占满画面。但4步Turbo仍会生成局部鼻梁和脸颊高光，因此适合作为新的预览/标准候选，不应宣称已达到最终电影级真实感。

随后完成的同Seed 8步Base实验没有改善真实感：耗时由16m24s上升到27m14s（+66%），面部亮斑占比由1.179%升至2.919%，纹理指标由47.17降至32.68。盲看接触表也显示8步版肤质更平滑、更偏蜡质，背景更规整。结论是“增加步数”不能单独解决油腻感，暂不继续盲跑25步。

## 联网核验

- MiniMax官方模型卡明确指出，H3-Context-IR对最终质量至关重要，并强烈建议接入或严格遵循Prompting Guidance；官方还将768P和2K Regenerate作为不同质量阶段。[MiniMax H3模型卡](https://huggingface.co/MiniMaxAI/MiniMax-H3)
- MiniMax官方H3 Prompt Writing规范要求Ref2VA使用`subject_definitions`、`summary`、`retention_analysis`、`detailed_description`、`overall_soundscape`、`non_diegetic_music`六段结构，并建议使用具体视觉/声音描述，少用`cinematic`、`beautiful`等抽象词。[官方Prompt Skill](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)
- H3 Director作者给出的原生质量基线是25步、`res_multistep + simple`、CFG 1.0、video/audio sigma shift 12/3；当前4步Turbo本质是速度档。[ComfyUI MiniMax H3 Director](https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director)
- 社区20次近景探索认为Realism People LoRA约0.40–0.55是较谨慎范围，但该组Prompt和Seed并不相同，作者明确说不能当因果A/B；可作为下一轮候选，不能直接升为默认。[H3 LoRA findings #69](https://github.com/mrhz1973/ai-video-director/issues/69)

## 实测结果

| 版本 | 主要变化 | 耗时 | 面部亮斑占比* | 面部纹理指标* | 观察 |
|---|---|---:|---:|---:|---|
| 原商业版 | 棚拍参考、营销式Prompt | 14m21s | 0.204% | 65.83 | 锐，但像广告棚拍/CG，背景整齐得不自然 |
| 自然办公室版 | 侧窗光参考、写实负面约束 | 16m11s | 0.987% | 47.90 | 背景更真实，但近景更油、更软，不通过 |
| 漫射光哑光版 | 闭口自然表情、大面积漫射光参考、写实约束 | 16m24s | 1.179% | 47.17 | 综合最自然，构图和环境改善；局部高光仍存在 |
| 8步Base版 | 同Seed、关闭Turbo LoRA、8步Base | 27m14s | 2.919% | 32.68 | 耗时+66%，高光增加、纹理下降，人工观察更蜡质，不通过 |

\* 亮斑占比用OpenCV人脸检测后统计低饱和高亮像素；纹理指标为人脸ROI的Laplacian方差。两项会受姿态、构图和光照影响，只用于辅助，不替代盲评。第三版虽然高亮像素没有下降，但高光面积更符合现场光照，整体“塑料广告脸”主观感受下降。

## 推荐生产方案

1. **标准档**：保留4步Turbo与768×1344，但新增`realism`预设；参考图门禁拒绝大面积环形灯眼神光、额头/鼻梁饱和亮斑、磨皮和纯色棚拍背景。
2. **Prompt层**：将业务端短Prompt转换成官方六段式Ref2VA结构，明确人物、视频、音频的保留/替换关系；不使用`commercial advertising quality`、`perfect skin`、泛化`cinematic`等词。
3. **高质量档**：8步Base已证实“更多步数”不会自动带来更真实肤质，暂不投入完整25步长跑。只有LoRA或参考图实验明确改善后，才用25步验证最终质量上限。
4. **LoRA实验档**：下一轮优先安装Realism People LoRA，严格做同图、同Prompt、同Seed的OFF/0.40/0.55矩阵；重点检查油光、身份、推近和构图漂移。
5. **后处理**：只做轻量肤色高光压缩和胶片颗粒，不能用重度磨皮或锐化；视频超分只能提升细节/边缘，不能从根因上修复塑料皮肤。
6. **质量门禁**：增加面部高光占比、肤色饱和剪切、时序闪烁、脸部纹理、身份一致性和人工双盲评分；单个Seed不能升级生产默认。

## 后续实验指标与升级门槛

每个候选至少使用5个Seed，涉及成功率或P95时至少20次；同一组必须固定源视频、参考图、Prompt、分辨率和Seed集合。单次实验只改变一个变量。

| 维度 | 指标 | 候选升级门槛 |
|---|---|---|
| 真实感 | 匿名人工MOS（皮肤、环境、材质，1～5分） | 均值比当前4步基线提高至少0.4，且低分率不增加 |
| 油腻/蜡质 | 人脸高光占比 + 人工块状镜面高光检出率 | 自动指标不劣化超过10%，人工异常率低于10% |
| 细节 | 人脸Laplacian纹理、过锐边缘比例 | 纹理不低于基线90%，且无伪锐化/毛孔贴图感 |
| 身份 | ArcFace/CAM++同类人脸嵌入余弦相似度 | 相对基线不下降超过0.03；关键帧身份失败为0 |
| 时序 | 人脸光流残差、亮度闪烁、身份帧间方差 | 相对基线改善至少10%，不能出现单帧跳脸 |
| 运动保持 | 与源视频关键点轨迹/镜头运动误差 | 不劣化超过10%，无明显动作重写 |
| 构图 | 人脸面积CV、主体中心轨迹漂移 | 不高于当前基线，避免异常推近和裁脸 |
| 背景 | 重复人物、肢体错误、文字伪影人工检出 | 严重缺陷为0；一般缺陷低于10%样本 |
| 性能 | 端到端P50/P95、峰值显存、每视频秒成本 | 质量档P95与费用需单独公布，不透明替换标准档 |

自动分数仅用于筛选和回归告警，最终以匿名A/B盲评为准。特别是高光比例会把真实现场高光与塑料油光混在一起，不能单独作为上线依据。

## 实验文件

- 三路同屏：`runtime_validation/h3-10s-remix-20260904/comparison-3way-realism-768.mp4`
- 原商业版：`output-ref2va-4step-768x1344.mp4`
- 自然办公室版：`output-ref2va-4step-768x1344-natural.mp4`
- 漫射光哑光版：`output-ref2va-4step-768x1344-matte-v2.mp4`
- 第二版参考图：`identity-reference-natural.png`
- 第三版参考图：`identity-reference-matte.png`
- 指标：`realism-metrics.json`
- 4步/8步左右对比：`comparison-4step-vs-8step.mp4`（左4步，右8步）
- 8步Base：`output-ref2va-base-8step-768x1344-natural-v3.mp4`
- 4步/8步指标：`realism-4v8-ab.json`

前两次8步Base直接ComfyUI任务因弹性Worker回收/重启丢失；第三次通过临时提高最小Worker数完成。基础设施问题不计为模型失败，但暴露出实验任务必须通过中台任务链路提交或显式保护Worker租约。
