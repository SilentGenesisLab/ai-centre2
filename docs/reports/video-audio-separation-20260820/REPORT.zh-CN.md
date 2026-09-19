# RTX 5090 多语言视频对白 / 音乐 / 音效分离实测报告

测试日期：2026-08-20（Asia/Shanghai）  
服务器：`ssh -p 2222 donxu@121.15.184.231`  
远端完整无损结果：`/home/donxu/ai-centre/runtime/validation/audio-separation-20260820`  
模型：Bandit v2，DnR v3 multilingual，`checkpoint-multi.slim.pt`  
模型 SHA-256：`ba9ba16504cd5d987a8c01a00307afba1340f251c18c70406c787bf76e2c4102`

## 一、结论

本轮共测试 14 条真实视频和 1 条可控葡萄牙语补充样本，总时长 1,075.31 秒。其中13条来自 `I:\self_tool\ai-centre2\data\videos`，另补充1条 Wikimedia Commons 的 CC BY 3.0 真实葡语视频；所有样本均未截短。Bandit 输出 `speech / music / sfx` 三轨，并额外合成 `background = music + sfx`。

结论为“可进入灰度，但泰语和强音乐中文样本必须增加主观盲听门禁”，不能直接宣布所有语种完全生产通过。

- 西班牙语：3/3 自动识别为 `es`，合计 577.7 秒；对白 ASR 词数保留率 99.9%，背景轨词泄漏率 0.28%，表现稳定。
- 葡萄牙语：原目录没有真实葡语，“巴西”目录3条实际均为西班牙语/墨西哥素材。已补充1条32.8秒真实 CC BY 3.0 葡语视频：原混音和Bandit对白均识别84词、背景0词、文本差异0.3%；可控葡语样本CER为0，对白SI-SDR提升9.44dB。单条真实样本通过自动指标，但样本量仍不足以覆盖葡语口音和场景分布。
- 泰语：3/3 正确识别为 `th`，合计 163.9 秒；对白词数保留率 94.9%，背景词泄漏率 0.51%。`fixture-006` 的原混音/对白轨 ASR 文本差异为 32.3%，需要人工复听确认是分离伪影还是 ASR 对音色变化敏感。
- 英语：两条有英语对白的样本词数保留率 97.5%，背景词泄漏率 0.50%。另有一条纯音乐和一条俄语歌曲，说明“英文”文件夹并不等于英语音轨。
- 中文：三条样本背景轨 ASR 均为 0 词。`fixture-011` 与 `fixture-012` 的音频完全相同；`fixture-013` 强音乐样本对白轨 ASR 字数增加，但文本差异为 27.4%，需要主观复核。
- 性能：15条总纯推理86.40秒；RTF P50=0.097、P95=0.158，即P95仍快于实时约6.3倍。单文件纯推理耗时P50=3.86秒、P95=14.22秒。
- GPU：批量大小 4 时，任务峰值 CUDA allocated 6,907.91 MiB、reserved 11,292 MiB。测试期间 GPU 1 整卡峰值约 26.5/32.6 GiB，没有停止 TTS、OCR 或 Face 服务。
- 输出完整性：三轨重构SNR平均44.81dB、最低33.57dB；没有时长、采样率或声道数变化。

这里的“人声”是电影/短视频对白 `speech`，不是通用歌声 stem。`fixture-007` 的俄语演唱被归入 `music`，符合 DnR 模型定义。如果业务需要单独提取歌唱人声，应另接音乐人声二轨模型，不能直接复用本结论。

## 二、总指标

| 指标 | 结果 | 说明 |
|---|---:|---|
| 真实视频 | 14 条 | 13条本地素材 + 1条CC BY 3.0真实葡语视频，均无截短 |
| 可控补充样本 | 1 条 | 葡语 TTS + 真实视频音乐，SNR 可计算 |
| 总时长 | 1,075.31 秒 | 约17.92分钟 |
| 总纯推理耗时 | 86.40 秒 | 不含模型加载、ASR和AAC编码 |
| 单文件耗时 P50 / P95 | 3.86 / 14.22 秒 | 长度不同，绝对耗时只用于本批次 |
| RTF P50 / P95 | 0.097 / 0.158 | 越低越快，1.0为实时 |
| 对白 ASR 词数保留率 | 98.15% | 排除纯音乐、俄语歌曲和可控样本；不是有真值准确率 |
| 背景轨 ASR 词泄漏率 | 0.34% | 低置信短词可能是ASR对音乐的幻觉 |
| 重构 SNR 平均 / 最低 | 44.81 / 33.57 dB | `mix` 对比三轨求和 |
| 任务 CUDA allocated 峰值 | 6,907.91 MiB | `batch_size=4` |
| 任务 CUDA reserved 峰值 | 11,292 MiB | PyTorch缓存预留，不等于有效张量 |
| 进程最大常驻内存 | 约 1.49–2.19 GiB | `/usr/bin/time -v` |

## 三、分语种结果

| 音轨类型 | 样本数 | 时长 | 语种置信度均值 | 对白词数保留率 | 背景词泄漏率 | 平均RTF | 重构SNR均值 | 判断 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 西班牙语 | 3 | 577.7s | 0.993 | 99.9% | 0.28% | 0.068 | 44.0dB | 自动指标通过 |
| 泰语 | 3 | 163.9s | 0.983 | 94.9% | 0.51% | 0.089 | 41.0dB | 条件通过，需复听006 |
| 英语对白 | 2 | 108.8s | 0.996 | 97.5% | 0.50% | 0.116 | 49.0dB | 自动指标通过 |
| 中文 | 3 | 142.3s | 0.994 | 101.1% | 0% | 0.092 | 46.6dB | 条件通过，需复听013 |
| 葡萄牙语真实视频 | 1 | 32.8s | 1.000 | 100% | 0% | 0.103 | 48.3dB | 自动指标通过，样本量仍少 |
| 葡萄牙语可控样本 | 1 | 14.2s | 0.987 | 100% | 0% | 0.160 | 33.6dB | 可控真值通过 |
| 俄语歌曲 | 1 | 18.6s | 0.745 | 对白轨0词 | 音乐轨16词 | 0.136 | 47.1dB | 歌声归音乐，符合DnR定义 |
| 无可识别语音 | 1 | 17.0s | — | 对白轨0词 | 背景轨0词 | 0.143 | 50.2dB | 纯音乐分流正确 |

### 可控葡萄牙语真值指标

葡语人声与真实视频音乐按“对白高于音乐 6 dB”混合。原混音、参考人声和参考背景均保留，因此该条可以计算真实分离指标。

| 指标 | 原混音 | 分离后 | 提升 |
|---|---:|---:|---:|
| 对白 SI-SDR | 6.00dB | 15.44dB | +9.44dB |
| 背景 SI-SDR | -6.01dB | 8.92dB | +14.93dB |
| 葡语 ASR CER | 0 | 0 | 无退化 |

对白在背景估计中的投影为 -24.04 dB，背景在对白估计中的投影为 -24.67 dB。

## 四、Bandit 三轨与 MDX 两轨 A/B

选取西班牙语 `fixture-003`、泰语问题样本 `fixture-006`、中文强音乐样本 `fixture-013`、可控葡语 `fixture-014` 和真实葡语 `fixture-015`，使用 `UVR-MDX-NET Inst HQ 5` 做两轨 `vocals / instrumental` 对照。MDX模型文件SHA-256为 `811cb24095d865763752310848b7ec86aeede0626cb05749ab35350e46897000`。

这不是完全同类模型比较：Bandit针对影视 `dialogue / music / effects`，MDX针对音乐 `vocals / instrumental`。Bandit使用PyTorch CUDA；当前服务器的ONNX Runtime只有CPU Provider，因此MDX使用CPU执行。下表RTF均按“进程启动、模型加载、推理和写文件”的完整墙钟计算，具有可比性；Bandit前文的纯推理RTF不含模型加载。

| 样本 | Bandit完整RTF | MDX完整RTF | Bandit对白保留/背景泄漏 | MDX人声保留/伴奏泄漏 | Bandit文本差异 | MDX文本差异 |
|---|---:|---:|---:|---:|---:|---:|
| 西语003 | 0.080 | 0.213 | 100.0% / 0% | 100.8% / 0% | 0% | 0.6% |
| 泰语006 | 0.113 | 0.253 | 93.4% / 0.31% | 92.3% / 0.15% | 32.3% | 31.5% |
| 中文013 | 0.116 | 0.248 | 106.3% / 0% | 106.3% / 0% | 27.4% | 22.6% |
| 可控葡语014 | 0.303 | 0.472 | 100% / 0% | 100% / 0% | 0% | 0% |
| 真实葡语015 | 0.167 | 0.329 | 100% / 0% | 100% / 0% | 0.3% | 1.1% |

5条MDX总墙钟92.54秒，完整RTF均值0.303，峰值RSS约3.77GiB；它没有使用GPU显存。Bandit同批完整墙钟约40.59秒。由于执行后端不同，这说明当前部署形态下Bandit更快，不等价于宣称GPU版MDX也会慢同样倍数。

可控葡语真值给出了更关键的质量差异：

| 模型 | 对白/人声 SI-SDR | 对白提升 | 背景/伴奏 SI-SDR | 背景提升 |
|---|---:|---:|---:|---:|
| Bandit v2 | 15.44dB | +9.44dB | 8.92dB | +14.93dB |
| MDX Inst HQ 5 | 8.96dB | +2.96dB | 9.60dB | +15.60dB |

MDX的伴奏抑制略优，但提取出的人声离干净真值更远；泰语006和中文013的ASR差异虽分别改善0.8和4.8个百分点，仍然明显异常，不能据此判定问题已解决。综合结论：影视对白生产默认继续使用Bandit；若业务目标是歌曲的人声/伴奏两轨，可把MDX作为独立可选模式，不能用它替代三轨DnR。MDX权重的独立商用许可本轮尚未取得可验证结论，确认前不得按代码仓库许可证推定权重可商用。

MDX试听：[西语003人声](mdx-ab/fixture-003/vocals.m4a) / [泰语006人声](mdx-ab/fixture-006/vocals.m4a) / [中文013人声](mdx-ab/fixture-013/vocals.m4a) / [可控葡语014人声](mdx-ab/fixture-014/vocals.m4a) / [真实葡语015人声](mdx-ab/fixture-015/vocals.m4a)。每个目录同时包含 `instrumental.m4a`。

## 五、逐样本结果和试听

试听文件为 160kbps AAC，仅用于快速 A/B；远端目录保留 48kHz 双声道 float WAV 无损结果。

| ID | 原目录 / 文件 | 实际音轨 | 时长 | 对白词保留 | 背景词数 | RTF | 重构SNR | A/B试听 |
|---|---|---|---:|---:|---:|---:|---:|---|
| fixture-001 | 巴西 / 墨西哥-30 | 西班牙语 | 140.7s | 416/416 | 0 | 0.071 | 45.3dB | [混音](previews/fixture-001/mix.m4a) / [对白](previews/fixture-001/speech.m4a) / [背景](previews/fixture-001/background.m4a) |
| fixture-002 | 巴西 / 墨西哥-9 | 西班牙语 | 237.7s | 743/744 | 5 | 0.066 | 40.8dB | [混音](previews/fixture-002/mix.m4a) / [对白](previews/fixture-002/speech.m4a) / [背景](previews/fixture-002/background.m4a) |
| fixture-003 | 巴西 / 墨西哥-优化-29 | 西班牙语 | 199.3s | 638/638 | 0 | 0.068 | 46.0dB | [混音](previews/fixture-003/mix.m4a) / [对白](previews/fixture-003/speech.m4a) / [背景](previews/fixture-003/background.m4a) |
| fixture-004 | 泰国 / 泰国-1 | 泰语 | 33.5s | 225/232 | 2 | 0.103 | 44.7dB | [混音](previews/fixture-004/mix.m4a) / [对白](previews/fixture-004/speech.m4a) / [背景](previews/fixture-004/background.m4a) |
| fixture-005 | 泰国 / 泰国-2 | 泰语 | 62.9s | 474/494 | 3 | 0.083 | 36.1dB | [混音](previews/fixture-005/mix.m4a) / [对白](previews/fixture-005/speech.m4a) / [背景](previews/fixture-005/background.m4a) |
| fixture-006 | 泰国 / 泰国-3 | 泰语 | 67.5s | 608/651 | 2 | 0.082 | 42.1dB | [混音](previews/fixture-006/mix.m4a) / [对白](previews/fixture-006/speech.m4a) / [背景](previews/fixture-006/background.m4a) |
| fixture-007 | 英文 / no-caption-1 | 俄语歌曲 | 18.6s | 0/16 | 16 | 0.136 | 47.1dB | [混音](previews/fixture-007/mix.m4a) / [对白](previews/fixture-007/speech.m4a) / [音乐](previews/fixture-007/music.m4a) |
| fixture-008 | 英文 / no-caption-2 | 纯音乐 | 17.0s | 0/0 | 0 | 0.143 | 50.2dB | [混音](previews/fixture-008/mix.m4a) / [对白](previews/fixture-008/speech.m4a) / [背景](previews/fixture-008/background.m4a) |
| fixture-009 | 英文 / no-caption-3 | 英语 | 94.3s | 339/349 | 2 | 0.076 | 46.2dB | [混音](previews/fixture-009/mix.m4a) / [对白](previews/fixture-009/speech.m4a) / [背景](previews/fixture-009/background.m4a) |
| fixture-010 | 英文 / AGE20 | 英语 | 14.6s | 49/49 | 0 | 0.157 | 51.8dB | [混音](previews/fixture-010/mix.m4a) / [对白](previews/fixture-010/speech.m4a) / [背景](previews/fixture-010/background.m4a) |
| fixture-011 | 中国 / test3 | 中文 | 39.8s | 183/183 | 0 | 0.097 | 52.8dB | [混音](previews/fixture-011/mix.m4a) / [对白](previews/fixture-011/speech.m4a) / [背景](previews/fixture-011/background.m4a) |
| fixture-012 | 中国 / test5 | 中文 | 39.8s | 183/183 | 0 | 0.097 | 52.8dB | [混音](previews/fixture-012/mix.m4a) / [对白](previews/fixture-012/speech.m4a) / [背景](previews/fixture-012/background.m4a) |
| fixture-013 | 中国 / 技术人学院宣传视频1 | 中文 | 62.6s | 84/79 | 0 | 0.083 | 34.3dB | [混音](previews/fixture-013/mix.m4a) / [对白](previews/fixture-013/speech.m4a) / [背景](previews/fixture-013/background.m4a) |
| fixture-014 | 可控葡语混音 | 葡萄牙语 | 14.2s | 38/38 | 0 | 0.160 | 33.6dB | [混音](previews/fixture-014/mix.m4a) / [对白](previews/fixture-014/speech.m4a) / [背景](previews/fixture-014/background.m4a) |
| fixture-015 | [Wikimedia CC BY 3.0](https://commons.wikimedia.org/wiki/File:Dados_pessoais_devem_ser_protegidos_pela_legisla%C3%A7%C3%A3o,_diz_conselheiro_da_Uni%C3%A3o_Europeia.webm) | 葡萄牙语 | 32.8s | 84/84 | 0 | 0.103 | 48.3dB | [混音](previews/fixture-015/mix.m4a) / [对白](previews/fixture-015/speech.m4a) / [背景](previews/fixture-015/background.m4a) |

每个试听目录还包含 `music.m4a` 和 `sfx.m4a`，可分别检查音乐和音效。

## 六、发现的问题

1. 本地素材仍没有真实葡语，不能因为目录叫“巴西”就按葡语计数；三条文件名标注墨西哥，ASR也均为高置信西班牙语。外部补充的1条CC BY 3.0真实葡语已通过自动指标，但不足以覆盖巴西葡语、欧洲葡语和不同背景噪声。
2. 泰语 `fixture-006` 的对白轨与原混音 ASR 差异偏大。背景泄漏很低，但对白可能存在音色/高频伪影，必须用泰语母语者复听开头、中间和结尾。
3. 中文 `fixture-013` 是强音乐样本，分离后 ASR 多识别出英文标题，但正文也发生变化。自动指标不能判断哪个版本更准确，需要结合脚本真值或人工听感。
4. `fixture-002` 对白 float WAV 峰值为 +0.486 dBFS，极少量样本超过 0 dBFS；`fixture-006` 也有轻微超峰。生产输出应增加 -1 dBTP 真峰值限制器，不能直接量化为 PCM16 后交付。
5. 歌声会被归入音乐。若调用方期望“任何人声都进入人声轨”，需要增加歌声模型和请求字段区分 `dialogue` 与 `vocals`。
6. `fixture-011` 和 `fixture-012` 的提取音频 SHA-256 相同，属于重复样本；统计报告保留两条，但不能当作两个独立音质分布。

## 七、生产建议

- 默认模型：Bandit v2 multilingual，三轨语义固定为 `dialogue / music / effects`。
- 输入：统一 48kHz 双声道 float32 推理；输出保存无损 WAV，外部试听或下载再转 AAC/FLAC。
- 并发：当前共用 GPU 1 时先设 worker 并发 1、模型内部 `inference_batch_size=4`。本轮任务预留约 11.3 GiB，GPU 1 与 TTS/OCR/Face 同卡时不应并发多个分离任务。
- 音量门禁：交付前加 -1 dBTP 限制器，保持响度，不做自动重度降噪。
- 自动门禁：时长/采样率/声道一致；重构 SNR ≥30dB；对白 ASR 语种一致；有对白时词数保留率 ≥95%；背景 ASR 词数比例 ≤1%。泰语等非空格语言还应增加字符级差异门禁。
- 人工门禁：每种语言至少抽听开头/中间/结尾；泰语 `fixture-006` 和中文 `fixture-013` 必听。当前报告没有伪造“已完成主观盲听”的结论。
- 模型许可：代码 Apache-2.0；多语言权重来自 Bandit v2/DnR v3 发布记录，权重许可为 CC BY-SA 4.0。正式商用前需要确认署名和相同方式共享义务。不要换用旧 Bandit Plus 的 CC BY-NC 权重做商用。

本轮已用MDX Inst HQ 5做5条代表样本A/B，但没有完成RoFormer或GPU版MDX对照，因此报告不声称Bandit是所有模型中的绝对最佳。当前数据足以支持Bandit进入受控灰度，但还不足以跳过泰语、强音乐和更多真实葡语验收。

## 八、数据文件

- `fixtures.json`：真实输入清单。
- `results.json`：逐样本完整指标、ASR正文和受控样本指标。
- `results.csv`：可在Excel中筛选的扁平指标。
- `resources.json`：每条任务CPU和内存记录。
- `asr/`：原混音、对白、背景三份ASR原始响应。
- `outputs/*/metrics.json`：每条推理原始指标。
- `previews/`：全部可试听的AAC A/B文件。
- `mdx-ab/`：MDX代表样本试听、ASR和资源记录。
- `mdx-ab-summary.json`：Bandit与MDX逐条对照和可控SI-SDR。
- `source-audio-v2/`：由真实视频提取的48kHz PCM输入。
- `PROCESS.zh-CN.md`：完整测试流程和复现方法。
