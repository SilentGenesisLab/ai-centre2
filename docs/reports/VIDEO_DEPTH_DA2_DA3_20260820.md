# RTX 5090 视频深度推理部署与测试报告

测试日期：2026-08-20（Asia/Shanghai）  
生产目录：`/home/donxu/ai-centre`  
模型目录：`/home/donxu/services/depth-models`  
测试视频：`input30.mp4`（544×960、30 FPS、900 帧、30 秒）

## 1. 验收结论

DA2 Small、DA2 Base、DA3 Small、DA3 Base 均已部署到 RTX 5090 生产节点，并已通过真实接口提交、GPU 推理、OSS 上传、任务查询和媒体完整性校验。

推荐默认使用 **DA3 Small**：本次完整 30 秒视频只占用 1.098 GB 峰值显存，API 闭环耗时 20.391 秒，是四种配置中显存最低、吞吐最高的组合。需要更强细节时使用 **DA3 Base**，峰值显存仍只有 2.110 GB。

四种模型不会同时驻留 GPU。Worker 采用单模型缓存，切换版本或规模时释放旧模型；下载与 OSS 上传可以并发，GPU 推理由进程内锁串行，避免多个任务同时推理造成 OOM。

生产实际从本机固定路径加载权重，没有在任务执行时访问 Hugging Face 或重新下载模型：

- DA2 Small：`/home/donxu/services/video-depth-anything-small/checkpoints/video_depth_anything_vits.pth`（116,440,756 bytes）
- DA2 Base：`/home/donxu/services/depth-models/da2/base/video_depth_anything_vitb.pth`（458,247,082 bytes）
- DA3 Small：`/home/donxu/services/depth-models/da3/small/model.safetensors`（137,248,940 bytes）
- DA3 Base：`/home/donxu/services/depth-models/da3/base/model.safetensors`（541,518,028 bytes）
- DA3 必要推理源码：`/home/donxu/services/depth-models/da3/source`；未克隆完整上游仓库。

## 2. 最终实测结果

| 版本 | 模型 | 有效输入尺寸 | 推理时间 | 处理时间 | API闭环时间 | 峰值显存 | 场景数 | 结果 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| DA2 | Small | 518 | 33.658s | 36.380s | 39.352s | 7.649GB | 19 | [查看视频](https://oss-imgai.sligenai.cn/ai-video-kernel/20260820/2d302718ff4041e6b68981f04ae996e6.mp4) |
| DA2 | Base | 392（自动限制） | 29.001s | 31.629s | 34.533s | 5.899GB | 19 | [查看视频](https://oss-imgai.sligenai.cn/ai-video-kernel/20260820/2696de5d2fef42b9b5f6cd7f0e2a2c7c.mp4) |
| DA3 | Small | 518 | 10.685s | 16.748s | 20.391s | 1.098GB | 19 | [查看视频](https://oss-imgai.sligenai.cn/ai-video-kernel/20260820/c3aac2d016254af6a234917932e4ba08.mp4) |
| DA3 | Base | 518 | 12.748s | 18.009s | 21.545s | 2.110GB | 19 | [查看视频](https://oss-imgai.sligenai.cn/ai-video-kernel/20260820/95c06aaa6ac9472fb2d21190bea2f493.mp4) |

说明：

- `推理时间`仅统计模型推理；`处理时间`还包含解码、分块、场景归一化和编码；`API闭环时间`进一步包含排队、状态传递和 OSS 上传。
- 当前 GPU1 同时驻留 AI Centre 的 ASR、TTS、OCR、Face 等生产服务。DA2 Base 在 518 输入下可能 OOM，因此生产自动限制到 392，并在结果中返回 `requested_input_size`、`effective_input_size` 和 `input_size_capped`。
- DA3 使用 12 帧分块、4 帧重叠及磁盘映射深度缓存，避免整段视频张量常驻显存和内存。
- 与 DA2 Small 相比，本次 DA3 Small 的峰值显存降低约 85.6%，API 闭环耗时降低约 48.2%。

## 3. 媒体与画面验证

四个 OSS 结果均经 FFprobe 全帧读取验证：

| 检查项 | 四个结果 |
|---|---|
| 编码 | H.264 / yuv420p |
| 分辨率 | 544×960 |
| 帧率 | 30 FPS |
| 帧数 | 900 |
| 时长 | 30.000 秒 |
| OSS 可访问性 | HTTP Range 206，完整下载成功 |

分别抽取第 0、10、20、29 秒进行人工检查。四种模型都保留人物、桌椅和背景的相对深度；自动检测到 19 个剪辑点并按场景归一化，后半段办公室镜头不再因全片统一归一化而接近全黑。

本机留存的最终视频和四宫格抽帧位于：

`runtime_depth_results/final_scene_aware_20260820/`

## 4. 接口契约

### 创建异步任务

```bash
curl -X POST "https://aicentre2.sligenai.cn:8443/v1/video-depth/jobs" \
  -H "Authorization: Bearer <SERVICE_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "source_uri": "https://storage.example.com/video/input.mp4",
    "version": "da3",
    "model": "small",
    "filename": "depth.mp4",
    "input_size": 518,
    "max_resolution": 960,
    "target_fps": -1
  }'
```

新增字段：

- `version`：`da2` 或 `da3`，默认 `da2`。
- `model`：`small` 或 `base`，默认 `small`。
- `input_size`：224～756且必须是14的倍数，默认518。
- `max_resolution`：输入最长边限制，224～1920，默认960。
- `target_fps`：`-1`保持原帧率，也可传1～60进行抽帧。

任务查询和取消：

```http
GET  /v1/video-depth/jobs/{job_id}
POST /v1/video-depth/jobs/{job_id}/cancel
```

需要高优先级等待并直接得到 OSS 结果时使用：

```http
POST /v1/video-depth/jobs/wait
```

请求体与异步接口一致，客户端超时建议设置为3600秒。

## 5. 部署与回归验证

- 后端单元测试：153项通过，1项按环境条件跳过。
- 生产服务：`ai-centre-control.service`、`ai-centre-depth-worker.service`、`ai-centre-admin.service` 均为 `active/running`，重启计数均为0。
- 公网后台：`/admin/` 正常跳转登录页，跟随跳转HTTP 200。
- 中文接口文档：`/admin/external-api.html` HTTP 200。
- OpenAPI：`/openapi.json` HTTP 200。
- 公网鉴权任务查询已验证，返回 DA3 Small 的完整任务状态及可直接访问的 OSS URL。

## 6. 测试流程复现

```bash
cd /home/donxu/ai-centre
PYTHONPATH=/home/donxu/ai-centre \
  .venv-control/bin/python scripts/validate_depth_api_matrix.py \
  --input runtime/deploy-staging/20260820/input30.mp4 \
  --report runtime/validation/depth-api-matrix-20260820/final-report.json

.venv-control/bin/python -m unittest discover -s tests -q
```

服务器原始结果：

`/home/donxu/ai-centre/runtime/validation/depth-api-matrix-20260820/final-report.json`

## 7. 已知限制

- 相对深度图用于几何层次和后续视觉处理，不提供真实米制距离。
- GPU 推理当前有意串行；Celery 线程并发2主要让素材下载、编码和上传交叠。若未来要让多个深度模型真正同时占用 GPU，需要独立显存配额和压力测试。
- DA2 Base 当前以392输入运行，不能将其结果直接当作“518输入的质量比较”；如果未来释放更多 GPU1 显存，可重新测试518。
