# AI 生成内容标识（对接说明）

给接入方的说明：中台生成的内容**带两类标识**——画面上看得见的「AI生成」，和文件里看不见但能读回的 AIGC 元数据。

两个都不需要你额外做什么：**隐式标识恒开**，显式标识用一个请求字段控制。

---

## 1. 一分钟接入

如果你已经在调生成接口，只需要**加一个字段**：

```jsonc
{
  "model": "nano-banana-2",
  "prompt": "……",
  "explicit_label": true      // ← 只有这个是新增的；不传 = false
}
```

`explicit_label` 在四个生成接口上都可用：

| 接口 | 效果 |
|---|---|
| `POST /v1/image-generations/jobs` | 左上角叠「AI生成」角标 |
| `POST /v1/video-generations/jobs` | 左上角叠「AI生成」角标（全片每一帧） |
| `POST /v1/text-generations/jobs` | 正文末尾追加可见尾注「【AI生成】」 |
| `POST /v1/audio-generations/jobs` | **无效**——音频只做隐式（见 §6） |

**默认是 `false`**，存量调用方行为不变。合规上更稳的做法是显式传 `true`。

> 处理类接口（视频超分、调色、唇形驱动、人脸处理、切片、去水印、图片超分）**不加标识** —— 它们不产生"生成合成内容"，是把用户已有的素材做加工。

---

## 2. 显式标识是什么样

### 图片 / 视频

左上角一个白字黑底的小角标，宽度取画面宽的 **12%**（上限 22%，避免小图上喧宾夺主），边距 2%。

- 视频是**逐帧叠加**的，不是只在开头出现（实测首帧与末帧都在）
- 角标是**预渲染的 PNG**，运行时只做 overlay。这样做的原因见 §6「为什么要预渲染」

### 文本

正文末尾单独一行：

```
把秋天的凉意，变成你手心的暖意。

【AI生成】
```

放**结尾**而不是开头——放开头会破坏「前 N 个字就是答案」这类下游用法。文案由服务端配置 `AIGC_TEXT_LABEL` 决定。

### 音频

没有显式标识。音频领域没有公认的可见标识做法，强行插入可听音只会牺牲听感。

---

## 3. 隐式标识（溯源数据）

写进**文件容器本身**，不改变画面与听感，但能被读回来核验。

| 格式 | 写在哪 |
|---|---|
| PNG | `iTXt` 块（关键字 `AIGC`，UTF-8） |
| JPEG | `COM` 段 |
| MP3 | ID3v2 的 `TXXX` 帧 |
| MP4 / MOV | `comment` 元数据 |
| 文本 | Unicode 标签字符（U+E0000–U+E007F），肉眼不可见 |

内容是这段 JSON：

```json
{ "AIGC": {
  "Label": "1",
  "ContentProducer": "<企业统一社会信用代码>",
  "ProduceID": "24465a2a66a7f1d965e468ff041694b1",
  "ReservedCode1": "4a4b54a8c658a699505dcebce9c9d625",
  "PropagatorID": "e2b93a26595d986bb3eff5dab98f834a",
  "ContentPropagator": "<企业统一社会信用代码>",
  "ReservedCode2": "hash:2e90a4b0836ac098dee4363aff4ea16b"
}}
```

| 字段 | 含义 | 我们放的值 |
|---|---|---|
| `Label` | 标识类型 | 固定 `"1"`＝属于生成合成内容 |
| `ContentProducer` | 生成合成服务提供者 | 企业统一社会信用代码（服务端环境变量注入） |
| `ProduceID` | 算法版本 + 内容 ID | `md5(算法版本 + 作业ID)` |
| `ReservedCode1` | 文件名 + 生成时间 | `md5(文件名 + 时间戳)` |
| `PropagatorID` | 时间戳 + 用户ID + 手机号 | **替代项**，见下 |
| `ContentPropagator` | 企业社会信用代码 | 与 `ContentProducer` 同值（服务端环境变量注入） |
| `ReservedCode2` | 信用代码的哈希 | `hash:md5(信用代码)` |

**`PropagatorID` 是替代实现，不是完整实现。** 规范要的是「时间戳 + 用户ID + 手机号」，而中台不知道调用方的用户是谁，只有 `external_ref`。当前用的是「时间戳 + external_ref」。**如果你们需要严格合规，要把真实的用户标识通过 `external_ref` 传进来**（或等平台补用户体系）。

---

## 4. 怎么核验

### 4.1 接口

`POST /v1/provenance/inspect`，三种输入**三选一**：

```bash
# a) 本地上传
curl -sS -X POST "$BASE_URL/v1/provenance/inspect" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@result.png"

# b) 公网 HTTPS 直链（比如中台 OSS 上的产物）
curl -sS -X POST "$BASE_URL/v1/provenance/inspect" \
  -H "Authorization: Bearer $API_KEY" \
  -F "url=https://oss.example.com/result.png"

# c) 直接贴一段文本（文本没有容器，标识就在字符里）
#    注意用 --data-urlencode 从文件取值：-F "text@file" 在 curl 里是「上传文件」，
#    不是「把文件内容当作 text 字段的值」。
curl -sS -X POST "$BASE_URL/v1/provenance/inspect" \
  -H "Authorization: Bearer $API_KEY" \
  --data-urlencode "text@marked.txt"
```

返回：

```json
{
  "filename": "result.png",
  "source": "url",
  "size": 2106995,
  "sha256": "f21598c1187fb3b5f858f62e…",
  "found": true,
  "container": "png",
  "where": "PNG iTXt",
  "payload": { "AIGC": { "Label": "1", "…": "…" } },
  "parseable": true,
  "raw": null
}
```

三个字段要分清：

- **`found`**：文件里有没有标识。`false` 是**正常答案**，不是错误。
- **`parseable`**：读到了内容但解不出 JSON（别人写的 `comment`，或格式坏了）。这时原文在 `raw` 里。
- **`source`**：`upload` / `url` / `text`，告诉你走的是哪条输入。

### 4.2 控制台

`生产任务 → 其他 → 内容标识核验`

- 三种输入可切（本地上传 / 公网 URL / 粘贴文本）
- 显示是否命中、写在哪、逐字段的 AIGC 内容、容器、字节数、sha256
- 有「重新检测」按钮，原样重发上一次输入

---

## 5. 覆盖范围

| 能力 | 显式 | 隐式 |
|---|:--:|:--:|
| 图像生成（gpt-image / nano-banana / midjourney…） | ✅ | ✅ |
| 视频生成（seedance / MiniMax H3） | ✅ | ✅ |
| 文本生成（deepseek flash） | ✅ | ✅ |
| 音乐与音效 | — | ✅ |
| 处理类（超分 / 调色 / 唇形 / 人脸 / 切片 / 去水印） | — | — |

---

## 6. 已知边界（对接前务必看）

### 6.1 标识依赖中台在本地留一份产物

生成结果会先落到中台自有 OSS 再给你 `result_urls`。**标识是在这一步做的**——没有本地文件就没有可标的东西。

如果部署没配 `KERNEL_UPLOAD_URL`，生成链路会直接透传上游链接，那条路上**不会带标识**。

### 6.2 下游二次转码会抹掉元数据

平台压缩、转封装、截图、上传到某些内容平台，都可能丢掉容器的 `comment` / `iTXt`。

**要长期溯源，请连原始文件一起留档**，别只存转码后的版本。

文本更脆弱：富文本编辑器、部分 API 网关会剥掉不可见字符。剥掉了就没了。

### 6.3 为什么不给角标用 drawtext

生产的 ffmpeg 是静态构建，**没有 `drawtext`、没有 libfreetype**，服务器上也没有任何中文字体。所以角标是**开发机上预渲染成 PNG**（`control_plane/assets/aigc-label.png`），运行时就只做 `overlay`。

换文案要重新烤一张图，不能改配置生效。

### 6.4 生成结果里没有「标识状态」字段

图片/视频/音频的作业结果**不返回**「这次到底标没标上」。要确认请用 §4 的核验接口。

文本生成是例外：它的结果里有 `aigc: {implicit, explicit, missing_fields}`，能直接看出标了什么。

### 6.5 企业信息未配置时会如实标注

`ContentPropagator` 只从服务端环境变量注入（不进公开仓库）。万一没配，隐式载荷仍会写 `Label` 与本地可推导的字段，但作业结果里会标注「企业信息未配置」——**不会静默写一份半截的**。

---

## 7. 常见问题

**Q：我只想要干净的画面，能关掉角标吗？**
能，不传 `explicit_label` 即可（默认 `false`）。但**隐式标识关不掉**——那是备案要求。

**Q：核验返回 `found: false`，是不是出错了？**
不是。说明这份文件里没写过标识（比如是你自己处理的素材，或下游转码时被抹掉了）。

**Q：同一份文件能不能重复标？**
可以。重复标注不会叠加，容器里始终只有一条记录（实测连写 4 次仍是 1 条）。

**Q：能拿到明文的水印内容吗？**
`payload` 里就是明文 JSON。哈希类字段（`ProduceID` 等）是不可逆的，但对账时拿 `job_id` 重算一次即可比对。

**Q：文本的标识会不会影响长度限制？**
会占字符数。实测一段 24 字的文本追加了 304 个不可见字符（一个 UTF-8 字节换一个标签字符）。如果下游有硬性字数限制，请用结果里的 `plain` 字段（不含不可见部分）。

**Q：中台改了算法版本，旧文件的标识还能读吗？**
能。`ProduceID` 只是把版本号混进哈希，不影响读取；但同一份内容重算出来的值会变，对账时要注意版本。

---

## 8. 给维护者

| 关注点 | 位置 |
|---|---|
| 载荷构造、容器读写、角标叠加 | `control_plane/provenance.py` |
| 三条生成链路的接入点 | `generation_tasks.py`（图片/视频）、`audio_generation_tasks.py`、`h3_tasks.py` |
| 文本生成（含标识） | `control_plane/text_generation_tasks.py` |
| 核验接口 | `control_plane/api.py` → `/v1/provenance/inspect` |
| 角标素材 | `control_plane/assets/aigc-label.png`（换文案要重新烤） |
| 企业信息配置 | `.env` 的 `AIGC_CONTENT_PRODUCER` / `AIGC_CONTENT_PROPAGATOR` / `AIGC_ALGORITHM_VERSION` / `AIGC_TEXT_LABEL` |
| 测试 | `tests/test_provenance.py` |

**加新生成型号时容易漏的一处**：`generation_tasks.OSS_PERSISTED_MODELS`。这张表是「转存 + 打标识」的共同前置，型号漏在外面表现为「功能全在、就是没标识」，而且不报错。`tests/test_ai_capabilities.py` 里有一条覆盖测试会挡住它。
