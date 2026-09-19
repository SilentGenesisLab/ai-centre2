# MiniMax H3 10秒人物与场景迁移实验报告

日期：2026-09-04  
实验环境：SeetaCloud，RTX 5090 32GB，ComfyUI 0.34.3  
目标：保留源视频前10秒的镜头运动、人物动作和节奏，替换人物、环境与背景。

## 结论

本次可用方案是 **Ref2VA + 参考视频 + 明确人物参考图 + 结构化关系提示词**。只传视频并通过文字要求更换人物，背景能成功迁移，但模型仍会继承源视频人物身份，达不到商业人物替换要求；增加人物参考图，并明确 `<Picture 1>` 的身份优先级高于 `<Video 1>` 后，人物和场景均成功替换，正脸至侧脸阶段身份稳定。

当前4步Turbo结果适合快速预览与业务验样。若用于最终投放，建议在通过构图审核后再进行同分辨率二采或H3 latent放大，避免每次草稿都承担二采成本。参考服务器的U17工作流值得复用其“双采、latent放大、音频修复”后处理思想，但它当前以FL2VA为主，不能直接作为视频人物迁移的主生成链路。

## 输入与配置

- 源视频：`2_火山字幕擦除.mp4` 前10秒
- 输入规格：1080×1920、30fps、AAC 44.1kHz双声道
- 推理模式：Ref2VA video remix
- 模型画布：480×864，9:16
- 输出：243帧、24fps、10.125秒、AAC 32kHz双声道
- Sampler：`res_multistep`
- Scheduler：`simple`
- Steps：4
- Seed：582904731
- UNET：`minimax/minimax_h3_ref2va_pruned_int8_convrot.safetensors`
- LoRA：`minimax/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`

官方模型卡说明H3支持4–15秒、24fps、32kHz立体声；Ref2VA支持最多9张图片、3段视频、3段音频，视频/音频总参考时长均不超过15秒。本次10秒输入处于官方范围内。[MiniMaxAI/MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3)

## A/B结果

| 方案 | 耗时 | 峰值显存 | 人物替换 | 场景替换 | 动作/镜头 | 结论 |
|---|---:|---:|---|---|---|---|
| 视频 + 文本 | 8m 32s | 未完整采集 | 失败，仍接近源亚洲人物 | 成功 | 保持较好 | 不满足商业要求 |
| 视频 + 人物参考图 + 文本 | 8m 43s | 23.28GiB | 成功，北美女性身份稳定 | 成功 | 保持较好 | 当前推荐基线 |

两次输出均为480×864、24fps、10.125秒。FFmpeg `blurdetect` 全片均值为：源片7.385、仅视频结果6.671、带人物参考图结果7.024。该指标只能用于同批次发现明显失焦，不能替代人工面部、手部和身份一致性审核。

### 视觉检查

- 人物：带参考图版本在正脸、近景、侧脸和背面阶段保持相同棕色中短发、浅蓝西装和西方面部特征。
- 环境：欧式豪宅稳定替换为冷色现代科技展厅，背景人群替换为商务观众。
- 动作：源片的向镜头靠近、表情变化、转头和向右转身顺序得到保留。
- 瑕疵：背景人物细节仍有生成感；画面只有约480p模型分辨率；生成音频不是对源音轨的逐采样无损拷贝。
- 文本：本次主动要求显示屏不出现可读文字或Logo，避免H3在小字上生成伪字。需要准确文字时应在生成后由剪辑/字幕层叠加。

## 为什么这样配置

MiniMax官方将FL2VA定义为文生、首帧、尾帧和首尾帧生成，将Ref2VA定义为图片、视频、音频的多模态参考生成；因此“基于一段视频更换人物和背景”必须优先走Ref2VA。[官方模型说明](https://huggingface.co/MiniMaxAI/MiniMax-H3)

ComfyUI H3 Director也明确：`t2v/i2v/fl2v` 使用FL2VA，`r2v/v2v/rv2v` 使用Ref2VA；其默认原生质量配置为25步、`res_multistep + simple`、CFG 1.0、video/audio sigma shift 12/3，并建议二采在一采构图确认后执行。[AIMixer H3 Director](https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director)

LightX2V公开的8步Turbo主要强调FL2V的画面与音频质量提升，不能据此推断它适合Ref2VA视频编辑。生产中必须按LoRA对应的任务族使用。[LightX2V MiniMax-H3 Turbo](https://huggingface.co/lightx2v/Minimax-h3-Turbo)

## 生产建议

1. 预览默认使用Ref2VA 4步、480×864；人物替换必须至少提供一张清晰、正面、无遮挡、与目标服装一致的参考图。
2. Prompt先定义引用关系，再写镜头保持项和替换项：`<Picture 1>`管身份，`<Video 1>`管动作、镜头和时序；不要只写“换成某国人物”。
3. 商业终稿采用两阶段：4步预览通过后，再以同一seed执行Ref2VA 8步或无Turbo 25步小规模A/B；二采不得切到FL2VA主模型。
4. 人物画面建议模型短边至少576/768；RTX 5090上10秒480×864已经需要约8m43s，提升到768p或二采需要单独测算吞吐。
5. 精确Logo、字幕和包装字不要交给生成模型，使用生成后确定性合成。
6. 调度侧将10秒Ref2VA预估槽位设为9分钟，并为状态接口增加瞬时连接超时重试；本次满载期间状态接口出现过一次短暂连接超时，但任务未失败。

## 文件

- `input-first-10s.mp4`：源片前10秒
- `identity-reference.png`：虚构人物参考图
- `output-ref2va-4step.mp4`：仅视频与文本结果
- `output-ref2va-4step-with-identity.mp4`：推荐结果
- `three-way-comparison.mp4`：源片、仅文本、带参考图三列对比
- `metrics-ref2va-4step-with-identity.json`：完整配置和显存采样
- `h3-reference-u17-ui.json`：参考服务器U17工作流快照

实验目录：`runtime_validation/h3-10s-remix-20260904/`

## 证据边界

本轮每种配置只运行1个固定seed，能够验证可行性和揭示人物参考图的决定性作用，但不能据此给出成功率、P95耗时或跨素材泛化结论。若要上线为稳定生产档，应再用至少10段人物/场景素材、每段3个seed做批量验收。

## 来源

- MiniMax AI，MiniMax H3官方模型卡，访问于2026-09-04：https://huggingface.co/MiniMaxAI/MiniMax-H3
- Comfy-Org，ComfyUI MiniMax H3工作流文档，访问于2026-09-04：https://docs.comfy.org/tutorials/video/minimax/minimax-h3
- AIMixer，ComfyUI MiniMax H3 Director README，访问于2026-09-04：https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director
- LightX2V，MiniMax-H3 Turbo模型卡，访问于2026-09-04：https://huggingface.co/lightx2v/Minimax-h3-Turbo
