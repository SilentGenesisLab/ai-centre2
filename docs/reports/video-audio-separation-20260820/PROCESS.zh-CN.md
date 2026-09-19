# 多语言视频音频分离测试流程

## 1. 目标

验证 5090 服务器对中文、英语、西班牙语、泰语和葡萄牙语的影视三轨分离能力，输出：

- `speech.wav`：对白。
- `music.wav`：音乐及歌唱内容。
- `sfx.wav`：音效。
- `background.wav`：`music + sfx`。

同时记录 ASR 可识别性、背景语音泄漏、三轨重构误差、速度、显存、CPU和内存。

## 2. 环境隔离

验证根目录：

```text
/home/donxu/ai-centre/runtime/validation/audio-separation-20260820
```

- GPU：物理 GPU 1，RTX 5090 32GB。
- PyTorch：复用 `.venv-tts-v026` 中已安装的 PyTorch 2.11.0+cu130 和 torchaudio，不安装或修改生产依赖。
- 模型代码：Bandit v2官方仓库的推理模型代码；不加载Ray、Lightning训练器或数据集代码。
- 模型权重：`checkpoint-multi.slim.pt`，仅保留模型state dict；参数来源与官方 `checkpoint-multi.ckpt` 相同。
- 测试期间未重启或停止 AI Centre 的 ASR、TTS、OCR、Face 等服务。

## 3. 输入准备

使用以下脚本枚举 `data/videos` 的全部文件，并把视频音轨统一为48kHz、双声道、PCM16 WAV：

```powershell
& .\scripts\prepare_audio_separation_fixtures.ps1
```

等价FFmpeg处理：

```bash
ffmpeg -i input.mp4 -vn -ac 2 -ar 48000 -c:a pcm_s16le fixture.wav
```

初次脚本版本使用中文目录名生成ID，在 Windows PowerShell 5 中遇到无BOM脚本编码问题，导致ID碰撞。模型尚未运行前已发现；最终版本改为与语言无关的全局 `fixture-001` 序号，并确认服务器收到13/13条音轨。

## 4. 推理参数

```text
sample_rate: 48000
chunk_size_seconds: 8
hop_size_seconds: 1
inference_batch_size: 4
stems: speech, music, sfx
device: CUDA_VISIBLE_DEVICES=1
dtype: float32
```

运行示例：

```bash
export CUDA_VISIBLE_DEVICES=1
/home/donxu/ai-centre/.venv-tts-v026/bin/python \
  run_bandit_v2_separation.py \
  --input inputs/source-audio-v2/fixture-001.wav \
  --output outputs/fixture-001 \
  --repo bandit-v2 \
  --checkpoint models/checkpoint-multi.slim.pt \
  --batch-size 4 \
  --device cuda
```

先用14.6秒英文样本做门禁：batch=1时RTF=0.256、allocated峰值1.85GiB；batch=4时RTF=0.161、allocated峰值6.91GiB，输出重构指标一致。全量测试采用batch=4。

## 5. ASR评估

对每条样本分别提交三次本机 faster-whisper large-v3：

```bash
curl -X POST http://127.0.0.1:9001/asr \
  -F file=@mix.wav -F beam_size=5

curl -X POST http://127.0.0.1:9001/asr \
  -F file=@speech.wav -F beam_size=5

curl -X POST http://127.0.0.1:9001/asr \
  -F file=@background.wav -F beam_size=5
```

记录：

- 自动语种与语种概率。
- 原混音、对白轨和背景轨的词数。
- word probability均值。
- 对白词数保留率。
- 背景轨词数/原混音词数。
- 原混音与对白轨的规范化字符编辑差异。

真实视频没有逐字真值，因此“词数保留率”不能当作CER。葡萄牙语补充样本使用已知文本，能够计算真实CER。

## 6. 葡萄牙语补充

目录中的“巴西”三条素材经ASR确认均为西班牙语。为验证葡语链路：

1. 使用本机AI Centre VoxCPM2生成已知葡语文本。
2. 取 `fixture-013` 分离出的真实音乐。
3. 将对白与音乐按对白高6dB混合，并保留两份干净参考源。
4. 对分离结果计算 SI-SDR、SI-SDR improvement 和相互投影串音。

该样本用于客观算法检查，报告中始终标记为合成可控样本，不与真实葡语视频混淆。

另从Wikimedia Commons取得1条明确标记为CC BY 3.0的真实葡语视频，完整测试32.8秒，不截取：

```text
external-fixtures/pt-real-cc-by-3.0.webm
SHA-256: 0DE0D7A7645F55CA7B123DAC1FC2507D292D9ACF67FEC49F28E88DAA1E0E8F9E
```

来源URL和许可证写入 `fixtures.json/results.json`；该样本用于补足真实语言链路，但单条样本不代表完整葡语分布。

## 7. 音频与资源指标

- 重构 SNR：`mix` 与 `speech + music + sfx` 的误差功率比。
- RMS dBFS、peak dBFS和超过0dBFS的样本比例。
- 时长、采样率、声道一致性。
- 推理wall time和RTF。
- `torch.cuda.max_memory_allocated/reserved`。
- `/usr/bin/time -v` 的进程最大RSS和CPU百分比。

受控样本额外计算：

- 对白和背景的SI-SDR。
- 相对混音的SI-SDR improvement。
- 对白在背景估计中的投影和背景在对白估计中的投影。

## 8. 试听产物

服务器保留48kHz float WAV完整输出，约1.5GB。为便于本机快速试听，另生成160kbps AAC：

```text
previews/<fixture_id>/mix.m4a
previews/<fixture_id>/speech.m4a
previews/<fixture_id>/music.m4a
previews/<fixture_id>/sfx.m4a
previews/<fixture_id>/background.m4a
```

AAC文件只用于A/B试听和报告交付，不参与重构、峰值或SI-SDR计算。

## 9. MDX代表样本A/B

使用 `UVR-MDX-NET Inst HQ 5` 对003、006、013、014、015做两轨对照。依赖只安装在验证目录的 `roformer-deps`，没有修改生产虚拟环境。当前 `onnxruntime.get_available_providers()` 只有 `CPUExecutionProvider`，因此报告明确按CPU基线记录。

```bash
export PYTHONPATH="$ROOT/roformer-deps"
export PATH="$ROOT/bin:/usr/local/bin:/usr/bin:/bin"
export CUDA_VISIBLE_DEVICES=1
bash "$ROOT/run_mdx_audio_separation_ab.sh"

python "$ROOT/analyze_audio_separation_ab.py" "$ROOT"
```

每条生成 `vocals / instrumental` WAV、AAC试听、两轨ASR、完整墙钟、CPU和RSS。可控葡语014额外用干净人声和背景计算SI-SDR。Bandit三轨和MDX两轨的语义不同，A/B只回答“当前影视对白场景下是否值得替换默认模型”，不把两者包装成同任务排行榜。

## 10. 汇总复现

```powershell
python .\scripts\analyze_audio_separation_results.py `
  .\docs\reports\video-audio-separation-20260820
```

脚本生成：

```text
results.json
results.csv
resources.json
mdx-ab-summary.json
```

输入和输出文件哈希包含在 `results.json/results.csv`，可用于确认复测使用的是同一素材。

## 11. 当前限制

- 已有1条真实葡萄牙语视频，但没有干净分轨真值，样本量也不足以覆盖口音和场景。
- 没有干净真实对白/音乐/音效分轨，因此真实视频不能计算绝对SDR，只能用ASR代理指标和三轨重构误差。
- 本轮未执行母语者盲听；需要人工复核泰语006和中文013。
- 已完成MDX CPU代表样本A/B，但没有运行RoFormer或GPU版MDX，因此不能给出跨模型冠军结论。
- MDX权重许可证尚未取得可验证的独立结论，不能仅依据工具代码许可证推定模型权重可商用。
- 歌唱内容属于music stem，而不是speech stem。
