from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CapabilityNotFound(KeyError):
    pass


class CapabilityStore:
    def __init__(self, path: Path, secret: str) -> None:
        self.path, self._lock = path, threading.RLock()
        self._key = hashlib.sha256(("ai-capabilities:" + secret).encode()).digest()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _init(self) -> None:
        with self._lock, self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS channels(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
              deployment_type TEXT NOT NULL, adapter TEXT NOT NULL, base_url TEXT NOT NULL DEFAULT '',
              credential_enc TEXT, credential_tail TEXT, auth_type TEXT NOT NULL DEFAULT 'none',
              enabled INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,
              timeout_seconds INTEGER NOT NULL DEFAULT 1800, health_status TEXT NOT NULL DEFAULT 'unknown',
              balance_json TEXT, last_checked_at TEXT, last_error TEXT, deleted_at TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS models(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
              capability_type TEXT NOT NULL, input_modalities_json TEXT NOT NULL,
              output_modality TEXT NOT NULL, parameter_schema_json TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_channels(
              id TEXT PRIMARY KEY, model_id TEXT NOT NULL, channel_id TEXT NOT NULL,
              upstream_model TEXT NOT NULL, submit_path TEXT NOT NULL, query_path TEXT NOT NULL,
              cancel_path TEXT, capabilities_json TEXT NOT NULL, parameter_map_json TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 100,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(model_id,channel_id), FOREIGN KEY(model_id) REFERENCES models(id), FOREIGN KEY(channel_id) REFERENCES channels(id));
            CREATE TABLE IF NOT EXISTS generation_jobs(
              id TEXT PRIMARY KEY, model_id TEXT NOT NULL, requested_channel TEXT NOT NULL,
              channel_id TEXT, status TEXT NOT NULL, stage TEXT NOT NULL, request_json TEXT NOT NULL,
              upstream_task_id TEXT, upstream_response_json TEXT, result_urls_json TEXT,
              fallback_count INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL,
              started_at TEXT, finished_at TEXT, elapsed_seconds REAL, updated_at TEXT NOT NULL,
              FOREIGN KEY(model_id) REFERENCES models(id), FOREIGN KEY(channel_id) REFERENCES channels(id));
            CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status,created_at);
            """)
            # Added after the first deployments: keep existing rows readable instead of rebuilding.
            columns = {row[1] for row in db.execute("PRAGMA table_info(generation_jobs)")}
            if "attempts_json" not in columns:
                db.execute("ALTER TABLE generation_jobs ADD COLUMN attempts_json TEXT")
            self._seed(db)

    def _seed(self, db: sqlite3.Connection) -> None:
        stamp = now()
        presets = [
            ("local", "本地部署", "local", "local_h3", "http://127.0.0.1:8320", 1, 10),
            # MOSS-Image 落在本机 GPU0（moss-image.service，127.0.0.1:8300），走 OpenAI 兼容的
            # 同步生图口。协议与 teamorouter 同形，但适配器单开一个值 —— 免得代码里
            # `adapter=="teamorouter"` 指向 localhost 这种看不懂的东西，也给 _compatible
            # 留一处按渠道压尺寸的地方。
            ("moss-local", "MOSS-Image 本地部署", "local", "local_openai_image", "http://127.0.0.1:8300", 1, 10),
            ("jmapi", "jmapi", "third_party", "jmapi", "", 0, 20),
            ("libtv", "libtv", "third_party", "libtv", "", 0, 30),
            ("grsai", "GRSAI", "third_party", "grsai", "https://grsai.dakka.com.cn", 0, 40),
            ("mxapi", "mxapi", "third_party", "mxapi", "https://open.mxapi.org", 0, 50),
            ("runninghub", "RunningHub", "third_party", "runninghub", "https://www.runninghub.cn", 0, 60),
            ("teamorouter", "TeamORouter", "third_party", "teamorouter", "https://api.teamorouter.com", 0, 70),
            ("wuyinkeji", "速创", "third_party", "wuyinkeji", "https://api.wuyinkeji.com", 0, 80),
            ("huanwangai", "幻网AI", "third_party", "huanwangai", "https://api.huanwangai.com", 0, 90),
            ("ark", "火山方舟", "third_party", "ark", "https://ark.cn-beijing.volces.com/api/v3", 0, 100),
            # 阿里云百炼（DashScope）的 OpenAI 兼容口。这里用的是**公共端点**
            # `dashscope.aliyuncs.com/compatible-mode/v1`，不是文档里那个带 WorkspaceId 的
            # MaaS 域名 —— 2026-10-28 用 sk-ws- 开头的业务空间 key 实测两者都能调通，
            # 公共端点少一个随工作区变化的变量。key 本身落在能力库里（AESGCM 加密），
            # 不进 git：它不是全账号的通用 key，换工作区就要换。
            ("dashscope", "阿里云百炼", "third_party", "dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1", 1, 110),
        ]
        for code, name, kind, adapter, url, enabled, priority in presets:
            db.execute("INSERT OR IGNORE INTO channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid4()),name,code,kind,adapter,url,None,None,"none",enabled,priority,1800,"unknown",None,None,None,None,stamp,stamp))
        # Safe preset migrations: fill only unconfigured endpoints and never overwrite an administrator value.
        db.execute("UPDATE channels SET base_url=?,updated_at=? WHERE code IN ('jmapi','libtv') AND base_url=''",
                   ("https://chorify3.sligenai.cn",stamp))
        # The current libtv reverse proxy does not require authentication.
        db.execute("UPDATE channels SET auth_type='none',credential_enc=NULL,credential_tail=NULL,updated_at=? WHERE code='libtv' AND credential_tail IS NULL",(stamp,))
        # mxapi 认的是 Authorization: Bearer <token>（文档里的 curl 就是这么带的）。
        db.execute("UPDATE channels SET auth_type='bearer',updated_at=? WHERE code='mxapi' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        # RunningHub 同样只认 Authorization: Bearer。它的 token 已经是部署级环境变量
        # （RUNNINGHUB_API_TOKEN，超分子系统在用同一个），所以这里**故意不落库**：
        # 不往能力库里复制第二份密钥，轮换只有一处。generation_tasks._headers 在
        # 渠道没有自己的凭据时回落到那个环境变量。管理员若要覆盖，在控制台填一次即可。
        db.execute("UPDATE channels SET auth_type='bearer',updated_at=? WHERE code='runninghub' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        # TeamORouter 也是 Authorization: Bearer。它的 key 和 RunningHub 不同，**可以**落在
        # 能力库里：控制台 PATCH /internal/admin/ai-capabilities/channels/{id} 写进来的凭据
        # 是 AESGCM 加密后存的（credential_enc），不进 git、不进前端。所以这里只把认证方式
        # 摆正，真正的 key 由运维从控制台注入，播种期 credential_tail 仍是 NULL。
        db.execute("UPDATE channels SET auth_type='bearer',updated_at=? WHERE code='teamorouter' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        # 速创（wuyinkeji）用的是**裸** `Authorization: <key>`，不带 `Bearer ` 前缀 ——
        # 这不是随意选择：它的**查询**端点带前缀会直接回 403「请求密钥KEY不正确！」
        # （2026-10-08 实测：提交端点三种写法都收，查询端点只认裸 Authorization，
        #   X-API-Key 也不认）。所以现有三种 auth_type 没有一个能用，新增 `authorization`。
        # key 与 TeamORouter 同理，可由控制台加密注入，播种期 credential_tail 为 NULL。
        db.execute("UPDATE channels SET auth_type='authorization',updated_at=? WHERE code='wuyinkeji' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        # 幻网 AI 同样是**裸** Authorization（`sk-mj-...`，不带 Bearer 前缀）。实测查询端点
        # 带不带前缀都收，但按上游文档 curl 的形式配，少一个变量。
        db.execute("UPDATE channels SET auth_type='authorization',updated_at=? WHERE code='huanwangai' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        # 火山方舟用标准的 `Authorization: Bearer <key>`。密钥本身不落库（复用
        # VIDEO_REVIEW_ARK_API_KEY），但 auth_type 必须摆正 —— 预设默认是 'none'，
        # 不改的话 _headers 即使回落到环境变量里的 key，最后也会因为 kind='none' 返回空头，
        # 表现为探针 401「key 无效」（2026-10-18 部署时踩到）。
        db.execute("UPDATE channels SET auth_type='bearer',updated_at=? WHERE code='ark' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        # 百炼同样是标准的 `Authorization: Bearer <sk-ws-...>`。key 由运维从控制台注入
        # （AESGCM 加密后存 credential_enc），播种期 credential_tail 仍是 NULL。
        db.execute("UPDATE channels SET auth_type='bearer',updated_at=? WHERE code='dashscope' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
        models = [
            ("minimax-h3", "MiniMax H3", "video_generation", ["text","image","video","audio"], "video"),
            ("minimax-h3-rh-enhanced", "MiniMax H3 RunningHub 增强版", "video_generation", ["text","image","video","audio"], "video"),
            ("seedance-2.0", "Seedance 2.0", "video_generation", ["text","image","video"], "video"),
            ("seedance-2.5", "Seedance 2.5", "video_generation", ["text","image","video"], "video"),
            ("gpt-image-2", "GPT Image 2", "image_generation", ["text","image"], "image"),
            ("gpt-image-2.5", "GPT Image 2.5", "image_generation", ["text","image"], "image"),
            ("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst", "image_generation", ["text","image"], "image"),
            ("gpt-image-2.5-flare", "GPT Image 2.5 Flare", "image_generation", ["text","image"], "image"),
            ("nano-banana-2", "Nano Banana 2", "image_generation", ["text","image"], "image"),
            ("nanobanana-2.1", "NanoBanana 2.1", "image_generation", ["text","image"], "image"),
            # Midjourney 只声明 text：它的「垫图/风格参考」要 base64 图片（`base64Array`），
            # 而中台这层拿到的是公网 URL，中间缺一次下载+编码。没做之前不声明 image，
            # 免得 auto 在有参考图的请求上把这一家选进来、再被 `_compatible` 筛掉。
            # 调用方仍可在 prompt 里写 `--sref <公网URL>`（MJ 自己去取），那条路不经过我们。
            ("midjourney", "Midjourney", "image_generation", ["text"], "image"),
            # MOSS-Image（OpenMOSS，Apache-2.0）本地部署：纯文生图，强在中文文字渲染
            # 与 JSON layout 排版。只声明 text —— 它没有参考图/图生图，声明了反而会被
            # auto 选进带参考图的请求、再被 _compatible 筛掉。
            ("moss-image", "MOSS Image", "image_generation", ["text"], "image"),
            # 第一种 text_generation 能力：自由文本生成（此前平台只有结构化判定）。
            ("deepseek-v4-flash", "DeepSeek V4.1 Flash", "text_generation", ["text"], "text"),
            # 百炼的两代 Omni。**只有 3.5-plus 能出音频**：3.8-flash 带 modalities 里的
            # audio 会回 400「does not support the modalities parameter containing audio」
            # （2026-10-28 实测），所以它的 output_modality 还是 text。
            # 3.5-plus 的 output_modality 写成 "text+audio" —— 这一列在库里是自由文本，
            # 没有枚举约束；语音是**可选**输出（请求里 output_audio=true 才要）。
            ("qwen3.5-omni-plus", "Qwen3.5 Omni Plus", "text_generation", ["text"], "text+audio"),
            ("qwen3.8-omni-flash", "Qwen3.8 Omni Flash", "text_generation", ["text"], "text"),
            # mxapi 的 Suno：一次生成出两首（两个 task），成品是 opus-in-mp4 的 .m4a
            ("suno-v6", "Suno v6 音乐", "audio_generation", ["text"], "audio"),
            ("suno-sound", "Suno 音效", "audio_generation", ["text"], "audio"),
            # Jev（TypeSafe）是**定型决策**模型，不是生成模型：给一段 state + 一组问题，
            # 回一组带置信度的定型答案。输入输出都是文本/JSON，所以 output_modality 是 json。
            # 这是平台里第一个 decision_generation 能力类型（见 decision_generation_tasks）。
            ("jev", "Jev 定型决策", "decision_generation", ["text"], "json"),
        ]
        schema = {"duration_seconds":{"type":"integer"},"resolution":{"type":"string"},"aspect_ratio":{"type":"string"}}
        for code,name,capability_type,inputs,output in models:
            db.execute("INSERT OR IGNORE INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid4()),name,code,capability_type,json.dumps(inputs),output,json.dumps(schema),1,None,stamp,stamp))
        db.execute("UPDATE models SET capability_type='image_generation',output_modality='image',updated_at=? WHERE code IN ('gpt-image-2','gpt-image-2.5','gpt-image-2.5-sunburst','gpt-image-2.5-flare','nano-banana-2')",(stamp,))
        audio_schema = {"prompt":{"type":"string"},"lyrics":{"type":"string"},"tags":{"type":"string"},"title":{"type":"string"},"instrumental":{"type":"boolean"},"loop":{"type":"boolean"}}
        db.execute("UPDATE models SET parameter_schema_json=?,updated_at=? WHERE code IN ('suno-v6','suno-sound')",(json.dumps(audio_schema),stamp))
        # Jev 的参数只有两个：state（自由文本）与 questions（问题字典，逐题带 type/instructions，
        # choice 与 score 还各带一份 criteria）。questions 的三种题型结构不同、且上游的校验
        # 比我们能表达的都细（choice 的 criteria 是对象、2~255 项；score 的 criteria 是**数组**、
        # 2~10 级；noul 不收 criteria），所以平台**原样透传**，不在这里复刻上游的题型校验。
        decision_schema = {"state":{"type":"string"},"questions":{"type":"object"}}
        db.execute("UPDATE models SET parameter_schema_json=?,updated_at=? WHERE code='jev'",(json.dumps(decision_schema),stamp))
        ids = {r["code"]:r["id"] for r in db.execute("SELECT id,code FROM channels")}
        mids = {r["code"]:r["id"] for r in db.execute("SELECT id,code FROM models")}
        bindings = [
          ("minimax-h3","local","minimax-h3","/v1/video-generations/minimax-h3/jobs","/v1/video-generations/minimax-h3/jobs/{task_id}",{"images":9,"videos":3,"audios":3},1,10),
          ("seedance-2.0","jmapi","seedance2.0_vip","/jmapi/v1/multimodal2video","/jmapi/v1/query",{"images":9,"videos":3,"audios":3,"duration_max":15},1,20),
          ("seedance-2.0","libtv","Seedance 2.0 VIP","/libtv/api/v1/video/publish","/api/v1/video/query/{task_id}",{"images":9,"videos":3,"audios":0,"duration_max":15},1,30),
          # Seedance 2.5 的 model_version 是 seedance2.5（不是 seedance2.5_vip），2026-09-22 实测自上游返回的
          # 版本白名单。上游另有两条硬约束在 generation_tasks._compatible 里拦：jmapi 只有 480p/720p。
          # 参考素材上限：jmapi 30 图实测（image_resource_id_list ≤ 30）；视频/音频取自渠道文档的 10，
          # 上游真正的闸门是「参考视频/音频总时长 ≤30s」（实测 11×4.9s 被拒）。libtv 侧不声明参考音频，
          # 沿用 2.0 的实测结论；需要参考音频时走 jmapi。
          ("seedance-2.5","jmapi","seedance2.5","/jmapi/v1/multimodal2video","/jmapi/v1/query",{"images":30,"videos":10,"audios":10,"duration_max":30},1,20),
          ("seedance-2.5","libtv","star-video2.5","/libtv/api/v1/video/publish","/libtv/api/v1/video/query/{task_id}",{"images":30,"videos":10,"audios":0,"duration_max":30},1,30),
          # RunningHub 的 MiniMax H3 增强版（ref2va = 参考素材 → 视频 + 音频）。上游字段是
          # 逐张编号的 refImage1..9 / refVideo1..3 / refAudio1..3，不是数组，映射在 _payload。
          # 三条硬约束都是 2026-09-29 用**空转探针**问上游要出来的（提交必被参数校验拒、
          # 不会建任务）：resolution 白名单是 480p/768p/1080p（**没有 720p**，但有 768p，
          # 正是 H3 的原生档）；duration 是 4～15 的整数；aspectRatio 白名单比我们开放的宽
          # （多 2:3/3:2/21:9），我们这边的五个值全在它白名单里，所以不构成缺口。
          ("minimax-h3-rh-enhanced","runninghub","minimax-h3-rh-enhanced","/openapi/v2/rhart-video/minimax-h3-rh-enhanced/ref2va","/openapi/v2/query",{"images":9,"videos":3,"audios":3,"duration_min":4,"duration_max":15,"resolutions":["480p","768p","1080p"]},1,60),
          ("gpt-image-2","grsai","gpt-image-2","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("gpt-image-2.5","grsai","gpt-image-2.5","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("gpt-image-2.5-sunburst","grsai","gpt-image-2.5-sunburst","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("gpt-image-2.5-flare","grsai","gpt-image-2.5-flare","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("nano-banana-2","grsai","nano-banana-2","/v1/draw/nano-banana","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          # 速创的查询是 GET + `?id=`，所以 task_id 直接拼进 query_path（中台按
          # `.replace("{task_id}",tid)` 处理），走通用 GET 分支，不需要在轮询里加分支。
          # `images:9` 沿用同门 nano-banana-2 的上限；速创自己的上限没实测过，
          # 只在提交侧验证过逗号分隔的多张参考图能被受理。
          ("nanobanana-2.1","wuyinkeji","NanoBanana2.1","/api/async/NanoBanana2.1","/api/async/detail?id={task_id}",{"images":9,"videos":0,"audios":0},1,80),
          # Midjourney（幻网 AI）：提交 POST /mj/submit/imagine、查询 GET /mj/task/{id}/fetch。
          # upstream_model 是 botType，同一个渠道以后加 Niji 就是再挂一行绑定、不改代码。
          # images:0 —— 这一版不做垫图（见模型清单里的说明）。
          ("midjourney","huanwangai","MID_JOURNEY","/mj/submit/imagine","/mj/task/{task_id}/fetch",{"images":0,"videos":0,"audios":0},1,90),
          # MOSS-Image：同步口（响应体里直接带 b64），没有 task id，所以 query_path 留空 ——
          # 与 teamorouter 走同一条 _sync_image_results 收结果。
          # image_sizes 是这条绑定的硬闸门：只开 1K/2K。4K 是 16.6 Mpx，本机 GPU0 跑不动
          # （实测 2048²=4.19 Mpx 峰值 11.6 GB 已是上限），让它在这里被 _compatible 挡掉，
          # auto 会去选别家、点名则 422，而不是让 MOSS 收下再 OOM 失败。
          ("moss-image","moss-local","moss-image","/v1/images/generations","",{"images":0,"videos":0,"audios":0,"image_sizes":["1K","2K"]},1,10),
          # 火山方舟（DeepSeek flash）：OpenAI 兼容的 /chat/completions，同步返回。
          # 型号用 260910 而不是最新的 261017 —— 后者账号没开通（ModelNotOpen），
          # 实测能调通的是 260910。
          ("deepseek-v4-flash","ark","deepseek-v4-1-flash-260910","/chat/completions","",{"images":0,"videos":0,"audios":0,"thinking_control":True},1,100),
          # 同一个逻辑型号（DeepSeek Flash）在百炼上的第二条绑定：上游型号名两边不一样，
          # 不能互相套用。priority 110 排在 ark(100) 之后 —— ark 在产线上是 online 的，
          # 这条是兜底；调用方传 channel=dashscope 才排在它前面。
          ("deepseek-v4-flash","dashscope","deepseek-v4.1-flash","/chat/completions","",{"images":0,"videos":0,"audios":0,"thinking_control":True},1,110),
          # Omni：`audio_output` 是**能力声明**，不是参数。请求里 output_audio=true 时
          # `_compatible` 只放行声明了它的绑定 —— 否则会把 3.8-flash（上游不支持音频）
          # 选进来再吃一个 400，白花一次调用。
          ("qwen3.5-omni-plus","dashscope","qwen3.5-omni-plus","/chat/completions","",{"images":0,"videos":0,"audios":0,"audio_output":True},1,110),
          ("qwen3.8-omni-flash","dashscope","qwen3.8-omni-flash","/chat/completions","",{"images":0,"videos":0,"audios":0},1,110),
          # TeamORouter 是 OpenAI 兼容的**同步**生图端点：POST /v1/images/generations 直接
          # 回 `data[0].b64_json`（PNG 的 base64），没有 task id、没有 status、没有 url 字段
          # （2026-09-30 实测，1K 约 37s）。所以这条绑定的 query_path 是空的——永远不会去轮询，
          # 由 generation_tasks 里 adapter=="teamorouter" 那条同步分支处理（解码 b64 → 传 OSS）。
          # `images: 0`：/v1/images/generations 只吃文本，不支持参考图（编辑走 /v1/images/edits，
          # 契约没实测过，不猜）。要参考图时 auto 会落到 grsai。
          # priority 70 = 排在 grsai(40) 之后：运维已定这个渠道先当兜底，不抢默认选路。
          ("gpt-image-2","teamorouter","gpt-image-2","/v1/images/generations","",{"images":0,"videos":0,"audios":0},1,70),
          ("gpt-image-2.5-sunburst","teamorouter","gpt-image-2.5-sunburst","/v1/images/generations","",{"images":0,"videos":0,"audios":0},1,70),
          ("gpt-image-2.5-flare","teamorouter","gpt-image-2.5-flare","/v1/images/generations","",{"images":0,"videos":0,"audios":0},1,70),
          # Jev：POST /v1/systemone，同步（实测 1.8s、$0.000014/次），同样没有 task id。
          # 上游型号名是带命名空间的 typesafe-ai/jev（`jev` 这个裸名没验证过，用已验证的那个）。
          ("jev","teamorouter","typesafe-ai/jev","/v1/systemone","",{"images":0,"videos":0,"audios":0},1,70),
          # Suno 的生成接口一次提交返回两个 task_id；两条绑定都查同一个 /api/v2/music/task。
          # 上游型号名就是文档里的 mv（灵感/自定义模式与音效的 mv 白名单不同，见 audio_generation_tasks）。
          ("suno-v6","mxapi","chirp-hawk","/api/v2/music/generate","/api/v2/music/task?id={task_id}",{"images":0,"videos":0,"audios":0},1,50),
          ("suno-sound","mxapi","chirp-crow","/api/v2/music/sound","/api/v2/music/task?id={task_id}",{"images":0,"videos":0,"audios":0},1,50),
        ]
        for model,channel,upstream,submit,query,caps,enabled,priority in bindings:
            db.execute("INSERT OR IGNORE INTO model_channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (str(uuid4()),mids[model],ids[channel],upstream,submit,query,None,json.dumps(caps),"{}",enabled,priority,stamp,stamp))
        db.execute("UPDATE model_channels SET upstream_model='seedance2.0_vip',capabilities_json=?,updated_at=? WHERE model_id=? AND channel_id=?",
                   (json.dumps({"images":9,"videos":3,"audios":3,"duration_max":15}),stamp,mids["seedance-2.0"],ids["jmapi"]))
        # DeepSeek 的推理开关是后加的：已有的绑定行是 INSERT OR IGNORE 建的，
        # 光改种子不会生效。只在「还没有这个键」时补，避免覆盖管理员在控制台改过的能力声明。
        for channel_code in ("ark", "dashscope"):
            row = db.execute(
                "SELECT capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids["deepseek-v4-flash"], ids[channel_code]),
            ).fetchone()
            if row is None:
                continue
            caps = json.loads(row[0] or "{}")
            if "thinking_control" in caps:
                continue
            caps["thinking_control"] = True
            db.execute(
                "UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE model_id=? AND channel_id=?",
                (json.dumps(caps), stamp, mids["deepseek-v4-flash"], ids[channel_code]),
            )
        db.execute(
            "UPDATE model_channels SET query_path=?,capabilities_json=?,updated_at=? WHERE model_id=? AND channel_id=?",
            (
                "/libtv/api/v1/video/query/{task_id}",
                json.dumps({"images":9,"videos":3,"audios":0,"duration_max":15}),
                stamp,
                mids["seedance-2.0"],
                ids["libtv"],
            ),
        )

    def _encrypt(self, value: str) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + AESGCM(self._key).encrypt(nonce, value.encode(), b"channel-secret")).decode()

    def decrypt(self, value: str | None) -> str:
        if not value: return ""
        raw = base64.b64decode(value); return AESGCM(self._key).decrypt(raw[:12], raw[12:], b"channel-secret").decode()

    @staticmethod
    def _channel(row: sqlite3.Row) -> dict[str, Any]:
        out=dict(row); out.pop("credential_enc",None)
        out["enabled"]=bool(out["enabled"]); out["credential_configured"]=bool(out.get("credential_tail"))
        for key in ("balance_json",): out[key[:-5]] = json.loads(out.pop(key)) if out.get(key) else None
        return out

    def channels(self, include_deleted: bool=False) -> list[dict[str,Any]]:
        q="SELECT * FROM channels" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY priority,name"
        with self._db() as db: return [self._channel(r) for r in db.execute(q)]

    def channel(self, code_or_id: str, *, private: bool=False) -> dict[str,Any]:
        with self._db() as db: row=db.execute("SELECT * FROM channels WHERE (id=? OR code=?) AND deleted_at IS NULL",(code_or_id,code_or_id)).fetchone()
        if not row: raise CapabilityNotFound(code_or_id)
        out=dict(row) if private else self._channel(row)
        if private: out["credential"] = self.decrypt(out.pop("credential_enc",None))
        return out

    def save_channel(self, data: dict[str,Any], channel_id: str|None=None) -> dict[str,Any]:
        stamp=now(); values=dict(data); credential=values.pop("credential",None)
        with self._lock,self._db() as db:
            if channel_id:
                old=db.execute("SELECT * FROM channels WHERE id=? AND deleted_at IS NULL",(channel_id,)).fetchone()
                if not old: raise CapabilityNotFound(channel_id)
                allowed={"name","deployment_type","adapter","base_url","auth_type","enabled","priority","timeout_seconds"}
                updates={k:v for k,v in values.items() if k in allowed}
                if updates.get("enabled") and old["health_status"] != "online": raise ValueError("channel must pass connection test before enabling")
                if credential is not None: updates.update(credential_enc=self._encrypt(credential) if credential else None,credential_tail=credential[-4:] if credential else None)
                updates["updated_at"]=stamp
                db.execute(f"UPDATE channels SET {','.join(k+'=?' for k in updates)} WHERE id=?",(*updates.values(),channel_id))
            else:
                cid=str(uuid4()); code=values["code"]
                db.execute("INSERT INTO channels(id,name,code,deployment_type,adapter,base_url,credential_enc,credential_tail,auth_type,enabled,priority,timeout_seconds,health_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cid,values["name"],code,values["deployment_type"],values["adapter"],values.get("base_url",""),self._encrypt(credential) if credential else None,credential[-4:] if credential else None,values.get("auth_type","none"),0,values.get("priority",100),values.get("timeout_seconds",1800),"unknown",stamp,stamp)); channel_id=cid
        return self.channel(channel_id)

    def record_probe(self, channel_id:str, ok:bool, error:str|None=None, balance:Any=None)->dict[str,Any]:
        with self._db() as db: db.execute("UPDATE channels SET health_status=?,last_error=?,balance_json=?,last_checked_at=?,updated_at=? WHERE id=?",("online" if ok else "offline",error,json.dumps(balance) if balance is not None else None,now(),now(),channel_id))
        return self.channel(channel_id)

    def delete_channel(self, channel_id:str)->None:
        if self.channel(channel_id)["code"] in {"local","jmapi","libtv","grsai","mxapi","runninghub","teamorouter","wuyinkeji","huanwangai","ark"}: raise ValueError("preset channel cannot be deleted; disable it instead")
        with self._db() as db: db.execute("UPDATE channels SET enabled=0,deleted_at=?,updated_at=? WHERE id=?",(now(),now(),channel_id))

    def models(self)->list[dict[str,Any]]:
        with self._db() as db:
            rows=db.execute("SELECT * FROM models WHERE deleted_at IS NULL ORDER BY name").fetchall()
            result=[]
            for r in rows:
                item=dict(r); item["enabled"]=bool(item["enabled"])
                item["input_modalities"]=json.loads(item.pop("input_modalities_json")); item["parameter_schema"]=json.loads(item.pop("parameter_schema_json"))
                binds=db.execute("SELECT mc.*,c.code channel_code,c.name channel_name,c.deployment_type,c.enabled channel_enabled,c.health_status FROM model_channels mc JOIN channels c ON c.id=mc.channel_id WHERE mc.model_id=? AND c.deleted_at IS NULL ORDER BY mc.priority",(r["id"],)).fetchall()
                item["channels"]=[]
                for b in binds:
                    x=dict(b); x["enabled"]=bool(x["enabled"]); x["channel_enabled"]=bool(x["channel_enabled"]); x["capabilities"]=json.loads(x.pop("capabilities_json")); x["parameter_map"]=json.loads(x.pop("parameter_map_json")); item["channels"].append(x)
                result.append(item)
            return result

    def binding(self, model_code:str, channel_code:str)->dict[str,Any]:
        with self._db() as db: row=db.execute("SELECT mc.*,m.code model_code,c.code channel_code,c.name channel_name,c.deployment_type,c.adapter,c.base_url,c.credential_enc,c.auth_type,c.timeout_seconds,c.health_status,c.enabled channel_enabled FROM model_channels mc JOIN models m ON m.id=mc.model_id JOIN channels c ON c.id=mc.channel_id WHERE m.code=? AND c.code=? AND m.deleted_at IS NULL AND c.deleted_at IS NULL",(model_code,channel_code)).fetchone()
        if not row: raise CapabilityNotFound(f"{model_code}:{channel_code}")
        out=dict(row); out["capabilities"]=json.loads(out.pop("capabilities_json")); out["parameter_map"]=json.loads(out.pop("parameter_map_json")); out["credential"]=self.decrypt(out.pop("credential_enc",None)); return out

    def binding_channels(self, model_code:str)->list[str]:
        """一个模型登记过的渠道，按绑定的 priority 排 —— 这就是 channel="auto" 的候选顺序。
        写死渠道名会让新模型进不了 auto，所以由绑定表本身说了算。"""
        with self._db() as db:
            rows=db.execute("SELECT c.code FROM model_channels mc JOIN models m ON m.id=mc.model_id JOIN channels c ON c.id=mc.channel_id WHERE m.code=? AND m.deleted_at IS NULL AND c.deleted_at IS NULL ORDER BY mc.priority",(model_code,)).fetchall()
        return [row[0] for row in rows]

    def channel_order(self, model_code:str, requested:str|None)->list[str]:
        """候选渠道顺序：点名的排头，同一型号的其余渠道按 priority 跟上。

        点名只是「先用它」，不是「只用它」—— 第三方抽风是常态，报错就接着试下一家，
        否则每挂一次都得人工换渠道重投。channel="auto"（或不给）就是纯 priority 顺序。
        没登记过的名字照样排头：能不能用由 binding() 说了算，这里不做判断。"""
        candidates=self.binding_channels(model_code)
        if not requested or requested=="auto": return candidates
        return [requested]+[code for code in candidates if code!=requested]

    def create_job(self, model_code:str, requested_channel:str, request:dict[str,Any])->str:
        models={m["code"]:m for m in self.models()}
        if model_code not in models or not models[model_code]["enabled"]: raise CapabilityNotFound(model_code)
        jid=str(uuid4()); stamp=now()
        with self._db() as db: db.execute("INSERT INTO generation_jobs(id,model_id,requested_channel,status,stage,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(jid,models[model_code]["id"],requested_channel,"queued","queued",json.dumps(request,ensure_ascii=False),stamp,stamp))
        return jid

    def update_job(self,jid:str,**values:Any)->None:
        values["updated_at"]=now()
        with self._db() as db: db.execute(f"UPDATE generation_jobs SET {','.join(k+'=?' for k in values)} WHERE id=?",(*values.values(),jid))

    def job(self,jid:str,include_request:bool=False)->dict[str,Any]:
        with self._db() as db: row=db.execute("SELECT j.*,m.code model,c.code channel FROM generation_jobs j JOIN models m ON m.id=j.model_id LEFT JOIN channels c ON c.id=j.channel_id WHERE j.id=?",(jid,)).fetchone()
        if not row: raise CapabilityNotFound(jid)
        out=dict(row); out["result_urls"]=json.loads(out.pop("result_urls_json") or "[]"); out["upstream_response"]=json.loads(out.pop("upstream_response_json") or "null"); out["attempts"]=json.loads(out.pop("attempts_json",None) or "[]")
        req=json.loads(out.pop("request_json")); out["request"]=req if include_request else {"prompt":{"present":bool(req.get("prompt")),"characters":len(req.get("prompt",""))},"reference_images":len(req.get("reference_image_urls",[])),"reference_videos":len(req.get("reference_video_urls",[])),"reference_audios":len(req.get("reference_audio_urls",[]))}
        return out

    def list_jobs(self,limit:int=100)->list[dict[str,Any]]:
        with self._db() as db: ids=[r[0] for r in db.execute("SELECT id FROM generation_jobs ORDER BY created_at DESC LIMIT ?",(limit,))]
        return [self.job(i) for i in ids]

    @staticmethod
    def _job_log_filters(*, capability_type:str|None=None, model:str|None=None, channel:str|None=None,
                         status:str|None=None, keyword:str|None=None, from_date:str|None=None,
                         to_date:str|None=None)->tuple[str,list[Any]]:
        clauses=["1=1"]; params:list[Any]=[]
        if capability_type: clauses.append("m.capability_type=?"); params.append(capability_type)
        if model: clauses.append("m.code=?"); params.append(model)
        if channel: clauses.append("c.code=?"); params.append(channel)
        if status: clauses.append("j.status=?"); params.append(status)
        if from_date: clauses.append("j.created_at>=?"); params.append(f"{from_date}T00:00:00")
        if to_date:
            end=(datetime.fromisoformat(to_date)+timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
            clauses.append("j.created_at<?"); params.append(end)
        if keyword:
            like=f"%{keyword}%"
            clauses.append("(j.id LIKE ? OR m.code LIKE ? OR c.code LIKE ? OR j.error LIKE ? OR j.request_json LIKE ?)")
            params.extend([like]*5)
        return " AND ".join(clauses),params

    @staticmethod
    def _reason_key(message:Any)->str:
        text=" ".join(str(message or "").split())
        return text[:120]+("…" if len(text)>120 else "")

    def query_job_logs(self,*,page:int=1,page_size:int=50,**filters:Any)->dict[str,Any]:
        """Paged job log rows without the heavy request/upstream payloads."""
        where,params=self._job_log_filters(**filters)
        page=max(1,int(page)); page_size=min(200,max(1,int(page_size)))
        with self._db() as db:
            total=db.execute(f"SELECT COUNT(*) FROM generation_jobs j JOIN models m ON m.id=j.model_id LEFT JOIN channels c ON c.id=j.channel_id WHERE {where}",params).fetchone()[0]
            rows=db.execute(
                f"""SELECT j.id,m.code model,m.name model_name,m.capability_type,
                           c.code channel,c.name channel_name,j.requested_channel,
                           j.status,j.stage,j.created_at,j.started_at,j.finished_at,
                           j.elapsed_seconds,j.fallback_count,j.error,j.result_urls_json,
                           j.attempts_json,j.upstream_task_id
                    FROM generation_jobs j
                    JOIN models m ON m.id=j.model_id
                    LEFT JOIN channels c ON c.id=j.channel_id
                    WHERE {where} ORDER BY j.created_at DESC LIMIT ? OFFSET ?""",
                [*params,page_size,(page-1)*page_size],
            ).fetchall()
        items=[]
        for row in rows:
            item=dict(row)
            item["result_urls"]=json.loads(item.pop("result_urls_json") or "[]")
            item["attempts"]=json.loads(item.pop("attempts_json",None) or "[]")
            items.append(item)
        return {"items":items,"total":total,"page":page,"page_size":page_size}

    def job_log_stats(self,**filters:Any)->dict[str,Any]:
        """Success rate, per-channel attempts, daily volume and top failure reasons."""
        where,params=self._job_log_filters(**filters)
        with self._db() as db:
            rows=db.execute(
                f"""SELECT j.id,m.code model,m.capability_type,c.code channel,c.name channel_name,
                           j.status,j.created_at,j.elapsed_seconds,j.error,j.attempts_json
                    FROM generation_jobs j JOIN models m ON m.id=j.model_id
                    LEFT JOIN channels c ON c.id=j.channel_id WHERE {where}""",
                params,
            ).fetchall()
            channel_names={row["code"]:row["name"] for row in db.execute("SELECT code,name FROM channels")}
        status_counts=Counter(row["status"] for row in rows)
        durations=[row["elapsed_seconds"] for row in rows if row["elapsed_seconds"] is not None]
        by_capability:dict[str,dict[str,Any]]={}
        by_channel:dict[str,dict[str,Any]]={}
        daily:dict[str,dict[str,Any]]={}
        reasons:Counter[str]=Counter()

        def channel_bucket(code:str,name:str|None)->dict[str,Any]:
            return by_channel.setdefault(code,{"channel":code,"channel_name":name or code,"attempts":0,"succeeded":0,"failed":0,"elapsed_ms":[]})

        for row in rows:
            capability=row["capability_type"] or "unknown"
            cap=by_capability.setdefault(capability,{"capability_type":capability,"total":0,"succeeded":0,"failed":0,"cancelled":0})
            cap["total"]+=1
            if row["status"] in ("succeeded","failed","cancelled"): cap[row["status"]]+=1
            day=(row["created_at"] or "")[:10]
            daily_row=daily.setdefault(day,{"date":day,"total":0,"succeeded":0,"failed":0,"cancelled":0})
            daily_row["total"]+=1
            if row["status"] in ("succeeded","failed","cancelled"): daily_row[row["status"]]+=1

            attempts=json.loads(row["attempts_json"] or "[]")
            if attempts:
                for attempt in attempts:
                    code=str(attempt.get("channel") or "unknown")
                    bucket=channel_bucket(code,channel_names.get(code))
                    bucket["attempts"]+=1
                    if attempt.get("outcome")=="success":
                        bucket["succeeded"]+=1
                        if attempt.get("elapsed_ms") is not None: bucket["elapsed_ms"].append(attempt["elapsed_ms"])
                    elif attempt.get("outcome")=="failed":
                        bucket["failed"]+=1
                        if attempt.get("error"): reasons[self._reason_key(attempt["error"])]+=1
            elif row["error"]:
                # Rows written before attempts_json existed only carry the joined error text.
                for part in str(row["error"]).split("; "):
                    code,_,message=part.partition(":")
                    code=(code.strip() or row["channel"] or "unknown") if message else (row["channel"] or "unknown")
                    bucket=channel_bucket(code,channel_names.get(code))
                    bucket["attempts"]+=1; bucket["failed"]+=1
                    if message: reasons[self._reason_key(message)]+=1
            elif row["channel"] and row["status"] in ("succeeded","failed"):
                bucket=channel_bucket(row["channel"],row["channel_name"])
                bucket["attempts"]+=1
                bucket["succeeded" if row["status"]=="succeeded" else "failed"]+=1

        def rate(succeeded:int,total:int)->float:
            return round(succeeded/total,4) if total else 0.0

        channel_rows=[]
        for bucket in by_channel.values():
            durations_ms=bucket.pop("elapsed_ms")
            bucket["success_rate"]=rate(bucket["succeeded"],bucket["attempts"])
            bucket["avg_elapsed_ms"]=round(sum(durations_ms)/len(durations_ms)) if durations_ms else None
            channel_rows.append(bucket)
        capability_rows=[]
        for bucket in by_capability.values():
            bucket["success_rate"]=rate(bucket["succeeded"],bucket["total"])
            capability_rows.append(bucket)
        total=len(rows); succeeded=status_counts.get("succeeded",0)
        return {
            "total":total,
            "succeeded":succeeded,
            "failed":status_counts.get("failed",0),
            "cancelled":status_counts.get("cancelled",0),
            "running":sum(status_counts.get(key,0) for key in ("queued","running")),
            "success_rate":rate(succeeded,total),
            "avg_elapsed_seconds":round(sum(durations)/len(durations),2) if durations else None,
            "by_channel":sorted(channel_rows,key=lambda item:-item["attempts"]),
            "by_capability":sorted(capability_rows,key=lambda item:-item["total"]),
            "daily":sorted(daily.values(),key=lambda item:item["date"]),
            "failure_reasons":[{"reason":reason,"count":count} for reason,count in reasons.most_common(10)],
        }
