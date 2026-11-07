from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 作业时间按 UTC 落库，页面上的日期筛选却是按北京时间输入的，比较前必须换算。
SHANGHAI = ZoneInfo("Asia/Shanghai")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@lru_cache(maxsize=1)
def _api_key_store():
    """账本库不存在（还没建过 Key）就不开——别为了结一笔不存在的账把目录造出来。"""
    from .api_keys import ApiKeyStore
    from .config import get_settings

    path = get_settings().api_keys_db_path
    return ApiKeyStore(path) if path.exists() else None


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
            # 提交幂等：api_key_id + idempotency_key（Idempotency-Key 头，缺省退回 external_ref）。
            # 唯一索引是幂等的真正保障——两次并发重试同时查不到、同时插入时，后一条会撞索引。
            if "api_key_id" not in columns:
                db.execute("ALTER TABLE generation_jobs ADD COLUMN api_key_id TEXT")
            if "idempotency_key" not in columns:
                db.execute("ALTER TABLE generation_jobs ADD COLUMN idempotency_key TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_jobs_idem "
                "ON generation_jobs(api_key_id,idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            self._seed(db)

    def _seed(self, db: sqlite3.Connection) -> None:
        stamp = now()
        # ── libtv 视频型号总表（2026-11-06）──────────────────────────────────────
        # 真值是上游 `POST /libtv/api/v1/video/models` 返回的 37 条，字段语义与其校验层
        # （LiptvApi/api-server/lib/models.js）一致。这里列 35 条：seedance-2.0 /
        # seedance-2.5 的绑定在下面已有的 bindings 里。
        #
        # 每行 = (上游 key, 中台 code, 名称, modes, ratios, resolutions,
        #         时长, 素材上限, 有音轨, 固定分辨率, 固定比例)
        #   modes      : 用 | 连接。发包时按参考素材挑一个（generation_tasks._libtv_mode）。
        #   ratios     : 上游白名单 ∩ 中台 aspect_ratio。空表示该型号只能 auto，靠固定比例。
        #   resolutions: 小写，上游白名单 ∩ 中台 resolution。中台没有 540p / 4k 两档，
        #                只有这两档的型号落到固定分辨率。
        #   时长       : (下限, 上限, 可选值列表)。
        #   素材上限   : (图, 视频, 音频)。
        # 这些数字上游**真的会校验**（validateParams 先校验再挑号，错了一分钱不花但作业会失败），
        # 所以照抄而不是拍脑袋；它们同时是 `_compatible` 的闸门，让 auto 与点名 422 在本地判准。
        libtv_video_catalog = [
            # 2026-11-06：Fast / Mini 两档上游只有 480p/720p（models.js 6bfafe4 已改），
            # 且 mixed 素材上限是 3视频+9图+3音频，不是 30/10/10 —— 照 2.0 VIP 抄的那两栏是错的。
            ("star-video2-fast","seedance-2.0-fast","Seedance 2.0 Fast VIP",
             "text2video|singleImage2video|frames2video|image2video|mixed2video|audio2video",
             ["16:9","4:3","1:1","3:4","9:16"],["480p","720p"],(4,15,None),(9,3,3),True,None,None),
            ("star-video2-mini","seedance-2.0-mini","Seedance 2.0 Mini",
             "text2video|singleImage2video|frames2video|image2video|mixed2video|audio2video",
             ["16:9","4:3","1:1","3:4","9:16"],["480p","720p"],(4,15,None),(9,3,3),True,None,None),
            ("seedance-1.5-pro","seedance-1.5-pro","Seedance 1.5 Pro",
             "text2video|singleImage2video|frames2video|image2video|mixed2video|audio2video",
             ["16:9","4:3","1:1","3:4","9:16"],["480p","720p","1080p"],(4,12,None),(30,10,10),True,None,None),
            ("doubao-seedance-pro","seedance-1.0-pro","Seedance 1.0 Pro",
             "text2video|singleImage2video",
             ["16:9","1:1","9:16"],["480p","720p","1080p"],(3,10,None),(1,0,0),True,None,None),
            ("doubao-seedance-lite","seedance-1.0-lite","Seedance 1.0 Lite",
             "text2video|singleImage2video",
             ["16:9","1:1","9:16"],["480p","720p","1080p"],(3,10,None),(1,0,0),True,None,None),
            # 2026-11-06 上游新增。Kling 4.0 的 CLI 只开放三个模式（单图/首尾帧还没放出来），
            # mixed 与 videoEdit 两模式下上游不收 ratio（modeSettings 里的 ratio_auto，
            # normalizeParams 会把它删掉、角度跟随输入素材），所以这里不需要 default_ratio。
            # 4.0 没有单图模式，默认落 mixed2video；allowAudioOnly 允许只给音频。
            ("kling-v4","kling-4.0","Kling 4.0",
             "text2video|mixed2video|videoEdit2video",
             ["16:9","1:1","9:16"],["720p","1080p"],(3,30,None),(10,5,3),False,None,None),
            # Flash 只出 720p、单图模式不收 ratio；mixed 不接受音频（audios=0）。
            ("kling-v4-flash","kling-4.0-flash","Kling 4.0 Flash",
             "text2video|singleImage2video|mixed2video|videoEdit2video",
             ["16:9","1:1","9:16"],["720p"],(3,20,None),(10,5,0),False,None,None),
            ("kling-v3-turbo","kling-3.0-turbo","Kling 3.0 Turbo",
             "singleImage2video|audio2video",
             ["16:9","1:1","9:16"],None,(3,15,None),(1,0,10),True,"pro",None),
            ("kling-v3-omni","kling-o3","Kling O3",
             "singleImage2video|frames2video|mixed2video|videoEdit2video|audio2video",
             ["16:9","1:1","9:16"],None,(3,15,None),(30,10,10),True,"auto",None),
            ("kling-video-o3","kling-3.0","Kling 3.0",
             "singleImage2video|frames2video|audio2video",
             ["16:9","1:1","9:16"],None,(3,15,None),(2,0,10),True,"auto",None),
            ("kling-video-o1","kling-o1","Kling O1",
             "singleImage2video|frames2video|mixed2video|videoEdit2video|audio2video",
             ["16:9","1:1","9:16"],None,(3,10,None),(30,10,10),True,"auto",None),
            ("kling-v2-6","kling-2.6","Kling 2.6",
             "singleImage2video|frames2video",
             ["16:9","1:1","9:16"],None,(3,10,None),(2,0,0),True,"auto",None),
            ("kling-v2-5-turbo-pro","kling-2.5","Kling 2.5",
             "singleImage2video|frames2video",
             ["16:9","1:1","9:16"],None,(3,10,None),(2,0,0),True,"auto",None),
            ("kling-2.1","kling-2.1","Kling 2.1",
             "singleImage2video",
             ["16:9","1:1","9:16"],None,(3,10,None),(1,0,0),True,"auto",None),
            ("kling-v3-motion-control","kling-3.0-motion-control","Kling 3.0 动作迁移",
             "singleImage2video",
             ["16:9","1:1","9:16"],None,(3,10,None),(1,1,0),False,"auto",None),
            ("MiniMax-Hailuo-H3-Max","minimax-h3-max","MiniMax H3 Max",
             "text2video|singleImage2video|frames2video",
             ["16:9","4:3","1:1","3:4","9:16"],["480p","768p"],(5,15,None),(2,0,0),False,None,None),
            # 2026-11-06 上游新增的标准 H3。上游 key 的别名里有 `minimax-h3`，但那个 code 已被
            # **本地 H3 池**（local 渠道）占用，不能复用它，否则这条会顶掉自建池的绑定 ——
            # 命名沿用 `minimax-h3-rh-enhanced` 的「渠道限定」写法。
            # 2K 是它的原生高档（上游 768P/2K 两档，没有 1080p，所以不吃 2k→1080p 的别名映射）。
            ("MiniMax-Hailuo-H3","minimax-h3-libtv","Minimax H3",
             "text2video|singleImage2video|frames2video|mixed2video",
             ["16:9","4:3","1:1","3:4","9:16"],["768p","2k"],(5,15,None),(9,3,3),False,None,None),
            # Highspeed（约 30s 出片）：只出 768P；比标准版多一个 image2video 模式。
            ("minimax-h3-hs","minimax-h3-hs","H3-Highspeed",
             "text2video|singleImage2video|frames2video|image2video|mixed2video",
             ["16:9","4:3","1:1","3:4","9:16"],["768p"],(4,15,None),(9,3,3),False,None,None),
            ("MiniMax-Hailuo-2.3","hailuo-2.3","Hailuo 2.3",
             "singleImage2video",
             [],["768p","1080p"],(None,None,[6,10]),(1,0,0),False,None,"auto"),
            ("MiniMax-Hailuo-2.3-Fast","hailuo-2.3-fast","Hailuo 2.3 Fast",
             "singleImage2video",
             [],["768p","1080p"],(None,None,[6,10]),(1,0,0),False,None,"auto"),
            ("MiniMax-Hailuo-o2","hailuo-o2","Hailuo 02",
             "singleImage2video",
             ["16:9","1:1","9:16"],["768p","1080p"],(3,6,None),(1,0,0),False,None,None),
            ("wanx2.7-video","wan-2.7","Wan 2.7",
             "frames2video|video2video|mixed2video|audio2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(2,10,None),(30,10,10),True,None,None),
            ("wanxiang-v2-6","wan-2.6","Wan 2.6",
             "text2video|singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(2,15,None),(1,0,0),True,None,None),
            ("wanxiang-preview","wan-2.5","Wan 2.5",
             "text2video|singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(2,10,None),(1,0,0),True,None,None),
            ("wanxiang-plus","wan-2.2","Wan 2.2",
             "text2video|singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(2,10,None),(1,0,0),True,None,None),
            ("viduq3-pro","vidu-q3-pro","Vidu Q3 Pro",
             "text2video|singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(1,16,None),(1,0,0),True,None,None),
            # text2video 上游声明了但**实测每次都失败**（720p/1080p × 2s/5s 三种组合都在 ~10s
            # 内回「视频生成失败，积分将会在2小时内返还」），所以不开放，只留单图生视频。
            ("viduq2-pro","vidu-q2-pro","Vidu Q2 Pro",
             "singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(1,10,None),(1,0,0),True,None,None),
            ("viduq2","vidu-q2","Vidu Q2",
             "text2video|singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(1,10,None),(1,0,0),True,None,None),
            ("viduq2-turbo","vidu-q2-turbo","Vidu Q2 Turbo",
             "singleImage2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(1,10,None),(1,0,0),True,None,None),
            ("happy-horse-1.1","happy-horse-1.1","Happy Horse 1.1",
             "frames2video|videoEdit2video|image2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(3,15,None),(30,1,0),False,None,None),
            ("happy-horse-1","happy-horse-1.0","Happy Horse 1.0",
             "frames2video|videoEdit2video|image2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(3,15,None),(30,1,0),False,None,None),
            ("pixverse-v5.5","pixverse-5.5","Pixverse V5.5",
             "text2video|singleImage2video|frames2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(None,None,[5,8,10]),(2,0,0),True,None,None),
            ("pixverse-v5","pixverse-5","Pixverse V5",
             "text2video|singleImage2video|frames2video",
             ["16:9","4:3","1:1","3:4","9:16"],["720p","1080p"],(None,None,[5,8,10]),(2,0,0),True,None,None),
            ("midjourney-video","midjourney-video","MJ Video",
             "singleImage2video",
             ["16:9","1:1","9:16"],["720p","1080p"],(3,10,None),(1,0,0),False,None,None),
            ("omnihuman-1.5","omnihuman-1.5","OmniHuman 1.5",
             "audio2video",
             ["16:9","1:1","9:16"],["720p","1080p"],(3,10,None),(1,0,10),True,None,None),
        ]
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
            # ads-agent 是 TeamORouter 上的另一个账户（org:126869，2026-11-04 单独申请）。
            # 单开一个渠道而不是覆盖 teamorouter 的 key：那样会把 gpt-image-2 / sunburst /
            # flare / 2.5 / jev 五条绑定一起迁到新账户上，余额与额度都换了一份，不可回滚。
            ("teamorouter-ads", "TeamORouter (ads-agent)", "third_party", "teamorouter", "https://api.teamorouter.com", 0, 71),
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
        # ads-agent 是同一种鉴权（Bearer），只是账户不同：同样只摆正认证方式，key 由控制台注入。
        db.execute("UPDATE channels SET auth_type='bearer',updated_at=? WHERE code='teamorouter-ads' AND auth_type='none' AND credential_tail IS NULL",(stamp,))
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
            # 4K 档：**严格照请求尺寸出图**（2026-11-03 横评 21/21 与请求逐像素一致，
            # 同批的 grsai 与 nano-banana-2.1 都会擅自改尺寸/宽高比）。声明 image ——
            # 它的垫图走 /v1/images/edits，实测能把参考图里的设备造型锁住。
            ("gpt-image-2-4k-upscale", "GPT Image 2 4K Upscale", "image_generation", ["text","image"], "image"),
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
            ("qwen3.5-omni-plus", "Qwen3.5 Omni Plus", "text_generation", ["text","image","video","audio"], "text+audio"),
            ("qwen3.8-omni-flash", "Qwen3.8 Omni Flash", "text_generation", ["text","image","video","audio"], "text"),
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
        # libtv 的 37 个视频型号：型号行由上面的 catalog 派生，不重复抄一遍上游事实。
        for _key, _code, _name, _modes, _ratios, _res, _dur, _media, _sound, _dres, _dratio in libtv_video_catalog:
            _m = set(_modes.split("|"))
            _inputs = ["text"] if "text2video" in _m else []
            if _m & {"singleImage2video","frames2video","image2video","mixed2video","videoEdit2video","video2video"}:
                _inputs.append("image")
            if _m & {"mixed2video","videoEdit2video","video2video"}:
                _inputs.append("video")
            # 素材上限为 0 的不声明输入：Kling 4.0 Flash 有 mixed2video 但不收音频，
            # 只看模式会把它标成「能听音频」，而绑定那边的 audios=0 又会把请求挡掉。
            if _media[2] and _m & {"audio2video","mixed2video"}:
                _inputs.append("audio")
            db.execute("INSERT OR IGNORE INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid4()),_name,_code,"video_generation",json.dumps(_inputs),"video",json.dumps(schema),1,None,stamp,stamp))
        # 2026-11-07：`free` 不是上游型号，而是 libtv 侧的**畅用池**型号 —— 它自己不带上游 key，
        # 由 libtv-api 按「可灵4.0 → Kling 4.0 Flash → Seedance 2.0 Fast → Minimax H3 →
        # H3-Highspeed → Seedance 2.0 Mini」的顺序，挑第一个持畅用卡且有空位的账号，全程 0 积分。
        # 能力声明取这批型号的**并集**，只作本地宽闸门；真正能不能接由 libtv-api 逐个候选校验
        # （模式/比例/时长/素材数量不匹配的候选会被跳过），失败不会退回付费型号。
        # 计费上 usage_rates 里给它一条 0 元 / source=free 的费率，调用方的 API Key 不扣钱。
        db.execute("INSERT OR IGNORE INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (str(uuid4()),"Free 畅用池","free","video_generation",
                    json.dumps(["text","image","video","audio"]),"video",json.dumps(schema),1,None,stamp,stamp))
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
          # images/videos/audios 是**多模态输入**上限：百炼的两个 Omni 都能理解图片/视频/音频
          # （2026-11-02 实测 3.8-flash 三种都通，见 text_generation_tasks 的说明），
          # 所以这里不再是 0。9/3/3 与视频生成那边同一套平台口径。
          ("qwen3.5-omni-plus","dashscope","qwen3.5-omni-plus","/chat/completions","",{"images":9,"videos":3,"audios":3,"audio_output":True},1,110),
          ("qwen3.8-omni-flash","dashscope","qwen3.8-omni-flash","/chat/completions","",{"images":9,"videos":3,"audios":3},1,110),
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
          # gpt-image-2.5 的兜底：grsai 余额打光时自动落到这里（priority 70 > grsai 的 40）。
          # 上游没有纯 gpt-image-2.5 这个型号，只有 -sunburst / -flare 两个变体，
          # 这里选 sunburst（甲方示例用的就是它）。画风与 grsai 的 2.5 不完全一致。
          ("gpt-image-2.5","teamorouter","gpt-image-2.5-sunburst","/v1/images/generations","",{"images":0,"videos":0,"audios":0},1,70),
          # 4K 档单独挂在 ads-agent 这个账户上（另一个 org、另一份余额）。
          # `images: 1`：垫图走 /v1/images/edits，契约是 JSON 的单个 `image` 字段
          # （2026-11-03 横评实测），不是数组也不是 multipart —— 多喂只会静默丢图，
          # 所以这里诚实报 1，让带两张以上参考图的请求在 _compatible 就被拦到别家。
          ("gpt-image-2-4k-upscale","teamorouter-ads","gpt-image-2-4k-upscale","/v1/images/generations","",{"images":1,"videos":0,"audios":0},1,71),
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
        # libtv 的 35 条新绑定（上游 37 条减去 Seedance 2.0/2.5 那两条）。capabilities 里多出来的几个键都是上游**真的会校验**的闸门：
        #   modes           —— 该型号接受哪些模式；发包时按素材挑一个（_libtv_mode），
        #                      接不了的模式在本地就判掉，不白跑一趟上游。
        #   ratios / resolutions —— 上游白名单 ∩ 中台取值。中台没有 540p，Vidu/Pixverse
        #                      那几条因此只开放 720p/1080p。
        #   duration_values —— 只有离散档位的型号（Hailuo 2.3 收 6/10，Pixverse 收 5/8/10）。
        #   default_resolution / default_ratio —— 没有可选档的型号（Kling 是 auto / std|pro，
        #                      Hailuo 2.3 比例只有 auto），发包时固定成型号自己那一档。
        # `2k` 是上游 canonicalizeResolution 的别名：型号有 1080p 时 2K 会被映射过去，
        # 所以白名单里跟着放开；没有 1080p 的型号（H3 Max）照旧不接 2K。
        for _key, _code, _name, _modes, _ratios, _res, _dur, _media, _sound, _dres, _dratio in libtv_video_catalog:
            _lo, _hi, _vals = _dur
            _caps = {"images": _media[0], "videos": _media[1], "audios": _media[2],
                     "modes": _modes.split("|"), "ratios": _ratios, "sound": _sound,
                     # sound_param 表示**上游 CLI 收 `enableSound` 这个顶层字段**。它不是
                     # `sound`（型号有没有声音）的同义词：libtv-api 的 normalizeParams 会给
                     # 所有 sound:true 的型号自动补 enableSound，而 CLI 只认自己 schema 里的
                     # 字段 —— 别的型号会直接 `校验失败: params 顶层不允许设置未知字段
                     # 「enableSound」`，节点都建不出来。目前只有 Seedance 那两个验证过，
                     # 其余型号一律发包时置 null（null 不是 undefined，上游就不会补默认值）。
                     "sound_param": _key in {"star-video2", "star-video2.5"}}
            if _res is not None:
                _caps["resolutions"] = _res + (["2k"] if "1080p" in _res else [])
            if _vals is not None:
                _caps["duration_values"] = _vals
            else:
                _caps.update({"duration_min": _lo, "duration_max": _hi})
            if _dres:
                _caps["default_resolution"] = _dres
            if _dratio:
                _caps["default_ratio"] = _dratio
            db.execute("INSERT OR IGNORE INTO model_channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (str(uuid4()),mids[_code],ids["libtv"],_key,
               "/libtv/api/v1/video/publish","/libtv/api/v1/video/query/{task_id}",
               None,json.dumps(_caps),"{}",1,30,stamp,stamp))
        # `free` 畅用池的绑定。capabilities 取六个候选型号的**并集**（kling-v4 /
        # kling-v4-flash / star-video2-fast / MiniMax-Hailuo-H3 / minimax-h3-hs /
        # star-video2-mini），只当本地宽闸门：真正的逐型号校验在 libtv-api 的
        # resolveAutoModel 里做，接不了这组参数的候选会被跳过。
        # 时长 3（kling-v4 下界）～30（kling-v4 上界），素材上限取最小公共值之外的最大值
        # （10 图 / 5 视频 / 3 音频，超出时由候选逐个判）。
        db.execute("INSERT OR IGNORE INTO model_channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (str(uuid4()),mids["free"],ids["libtv"],"free",
           "/libtv/api/v1/video/publish","/libtv/api/v1/video/query/{task_id}",
           None,json.dumps({
               "images": 10, "videos": 5, "audios": 3,
               "modes": ["text2video","singleImage2video","frames2video","image2video",
                         "mixed2video","videoEdit2video","audio2video"],
               "ratios": ["16:9","4:3","1:1","3:4","9:16","21:9"],
               "resolutions": ["480p","720p","768p","1080p","2k"],
               "sound": False, "sound_param": False,
               "duration_min": 3, "duration_max": 30}),"{}",1,30,stamp,stamp))
        # seedance-2.0 / seedance-2.5 的 libtv 绑定早于这套能力声明，INSERT OR IGNORE 不会
        # 更新它们 —— 光改上面的种子不生效。只在缺 modes 时补，管理员改过的不覆盖。
        for _code, _modes, _ratios, _res, _dur in [
            ("seedance-2.0", "text2video|singleImage2video|frames2video|image2video|mixed2video|audio2video",
             ["16:9","4:3","1:1","3:4","9:16"], ["480p","720p","1080p"], (4, 15)),
            ("seedance-2.5", "text2video|singleImage2video|frames2video|image2video|mixed2video|audio2video|videoEdit2video",
             ["16:9","4:3","1:1","3:4","9:16"], ["480p","720p","1080p"], (4, 30)),
        ]:
            _row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[_code], ids["libtv"]),
            ).fetchone()
            if _row is None:
                continue
            _caps = json.loads(_row[1] or "{}")
            if "modes" in _caps:
                continue
            _caps.update({"modes": _modes.split("|"), "ratios": _ratios,
                          "resolutions": _res + ["2k"], "sound": True,
                          "duration_min": _dur[0], "duration_max": _dur[1]})
            db.execute("UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                       (json.dumps(_caps), stamp, _row[0]))
        # sound_param 是后加的键，上面那两行早就写过 capabilities_json 了，补一次（只在缺键时补）。
        for _code in ("seedance-2.0", "seedance-2.5"):
            _row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[_code], ids["libtv"]),
            ).fetchone()
            if _row is None:
                continue
            _caps = json.loads(_row[1] or "{}")
            if "sound_param" in _caps:
                continue
            _caps["sound_param"] = True
            db.execute("UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                       (json.dumps(_caps), stamp, _row[0]))
        # 2026-11-04：libtv-api 的型号目录是写死的，ratios / durations 两栏和 CLI 实际的
        # 校验规则对不上（报错里会把可选值列出来）。上面是 INSERT OR IGNORE，已经建好的
        # 绑定不会跟着改，这里按 CLI 实测结果覆盖一次；打了标记就不再动，管理员改过的保留。
        _cli_caps = {c[1]: (c[4], c[6], c[10]) for c in libtv_video_catalog}
        for _code in ("hailuo-o2", "vidu-q2-pro", "vidu-q2-turbo", "wan-2.2", "wan-2.5", "kling-o1"):
            _row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[_code], ids["libtv"]),
            ).fetchone()
            if _row is None:
                continue
            _caps = json.loads(_row[1] or "{}")
            if _caps.get("cli_verified"):
                continue
            _ratios, _dur, _dratio = _cli_caps[_code]
            _caps["ratios"] = _ratios
            if _dur[2] is not None:
                _caps["duration_values"] = _dur[2]
                _caps.pop("duration_min", None)
                _caps.pop("duration_max", None)
            else:
                _caps.update({"duration_min": _dur[0], "duration_max": _dur[1]})
                _caps.pop("duration_values", None)
            if _dratio:
                _caps["default_ratio"] = _dratio
            else:
                _caps.pop("default_ratio", None)
            _caps["cli_verified"] = "2026-11-04"
            db.execute("UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                       (json.dumps(_caps), stamp, _row[0]))
        # 2026-11-04 第二轮实测：libtv-api 的 normalizeParams 会保留中台显式传来的字面值，
        # 只在这三个型号上完全无法满足 —— Kling 2.5 / OmniHuman 1.5 的 CLI 只收 auto，
        # 而中台 HTTP 枚举不接受 auto，唯一出口是让绑定自己带 default_ratio/default_resolution。
        # 另外 Vidu Q2 Pro/Turbo 的 text2video 上游每次都失败（换参考图走单图生视频就正常），
        # 所以把 text2video 从能力里摘掉，免得调用方白烧一次积分。
        _sync = {c[1]: c for c in libtv_video_catalog}
        for _code in ("kling-2.5", "omnihuman-1.5", "vidu-q2-pro", "vidu-q2-turbo"):
            _row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[_code], ids["libtv"]),
            ).fetchone()
            if _row is None:
                continue
            _caps = json.loads(_row[1] or "{}")
            if _caps.get("cli_sync") == "2026-11-04d":
                continue
            _c = _sync[_code]
            _caps["modes"] = _c[3].split("|")
            _caps["ratios"] = _c[4]
            _caps.update({"images": _c[7][0], "videos": _c[7][1], "audios": _c[7][2]})
            if _c[5] is None:
                _caps.pop("resolutions", None)
            else:
                _caps["resolutions"] = _c[5]
            # 序号 9 是「固定分辨率」、10 是「固定比例」，别写反（写反会把 auto 塞错字段）。
            if _c[9]:
                _caps["default_resolution"] = _c[9]
            else:
                _caps.pop("default_resolution", None)
            if _c[10]:
                _caps["default_ratio"] = _c[10]
            else:
                _caps.pop("default_ratio", None)
            _caps["cli_sync"] = "2026-11-04d"
            db.execute("UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                       (json.dumps(_caps), stamp, _row[0]))
        # 2026-11-06：Seedance 2.0 Fast / Mini 的上游白名单早就是 480p/720p、mixed 素材上限是
        # 3视频+9图+3音频（种子当初照 2.0 VIP 抄成了 1080p / 30·10·10），已建好的绑定不会
        # 跟着改。按 catalog 覆盖一次；打了标记就不再动，管理员改过的保留。
        for _code in ("seedance-2.0-fast", "seedance-2.0-mini"):
            _row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[_code], ids["libtv"]),
            ).fetchone()
            if _row is None:
                continue
            _caps = json.loads(_row[1] or "{}")
            if _caps.get("libtv_sync") == "2026-11-06":
                continue
            _c = _sync[_code]
            _caps["resolutions"] = _c[5]
            _caps.update({"images": _c[7][0], "videos": _c[7][1], "audios": _c[7][2]})
            _caps["libtv_sync"] = "2026-11-06"
            db.execute("UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                       (json.dumps(_caps), stamp, _row[0]))
        # 2026-11-06 第二轮：上游把参数校验换成按模式的白名单（modeSettings / paramCapability，
        # commit e56fab3），几个型号的 `default_ratio="auto"` 反而成了必拒 —— validateParams
        # 现在会拿它与型号自己的比例白名单逐字比对，而那几家早就写的是 16:9/1:1/9:16。
        # `omnihuman-1.5` 的 `default_resolution="auto"` 同理（上游是 720P/1080P）。
        # 另外四个型号的时长白名单从中台的离散档改回了区间（Kling O1 / Wan 2.5 / Wan 2.2）。
        # 按 catalog 整体覆盖一次；打了标记就不再动，管理员改过的保留。
        for _code in ("kling-o1", "kling-2.5", "hailuo-o2", "wan-2.2", "wan-2.5",
                      "vidu-q2-pro", "vidu-q2-turbo", "omnihuman-1.5"):
            _row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[_code], ids["libtv"]),
            ).fetchone()
            if _row is None:
                continue
            _caps = json.loads(_row[1] or "{}")
            if _caps.get("libtv_sync") == "2026-11-06b":
                continue
            _c = _sync[_code]
            _lo, _hi, _vals = _c[6]
            _caps["modes"] = _c[3].split("|")
            _caps["ratios"] = _c[4]
            _caps.update({"images": _c[7][0], "videos": _c[7][1], "audios": _c[7][2]})
            if _c[5] is None:
                _caps.pop("resolutions", None)
            else:
                _caps["resolutions"] = _c[5] + (["2k"] if "1080p" in _c[5] else [])
            if _vals is not None:
                _caps["duration_values"] = _vals
                _caps.pop("duration_min", None)
                _caps.pop("duration_max", None)
            else:
                _caps.update({"duration_min": _lo, "duration_max": _hi})
                _caps.pop("duration_values", None)
            for _slot, _field in ((9, "default_resolution"), (10, "default_ratio")):
                if _c[_slot]:
                    _caps[_field] = _c[_slot]
                else:
                    _caps.pop(_field, None)
            _caps["libtv_sync"] = "2026-11-06b"
            db.execute("UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                       (json.dumps(_caps), stamp, _row[0]))
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
        # 只修 query_path：早先种子把它写成了不带 `/libtv` 前缀的 `/api/v1/video/query`。
        # 这里**不再重写 capabilities_json** —— 那一版会覆盖上面刚补的 modes 等能力声明，
        # 也会把管理员在控制台的改动每次重启都冲掉。
        db.execute(
            "UPDATE model_channels SET query_path=?,updated_at=? WHERE model_id=? AND channel_id=?",
            (
                "/libtv/api/v1/video/query/{task_id}",
                stamp,
                mids["seedance-2.0"],
                ids["libtv"],
            ),
        )
        # 多模态输入是后加的能力（2026-11-02）：老库里两条 Omni 绑定写死的是 0/0/0，
        # 光改上面的种子不生效（INSERT OR IGNORE 不会更新已有行）。**只在三个都是 0 时**
        # 抬到 9/3/3 —— 管理员在控制台手工调过的绑定不覆盖。
        for model_code in ("qwen3.5-omni-plus", "qwen3.8-omni-flash"):
            row = db.execute(
                "SELECT id,capabilities_json FROM model_channels WHERE model_id=? AND channel_id=?",
                (mids[model_code], ids["dashscope"]),
            ).fetchone()
            if row is None:
                continue
            caps = json.loads(row[1] or "{}")
            if any(int(caps.get(key) or 0) for key in ("images", "videos", "audios")):
                continue
            caps.update({"images": 9, "videos": 3, "audios": 3})
            db.execute(
                "UPDATE model_channels SET capabilities_json=?,updated_at=? WHERE id=?",
                (json.dumps(caps), stamp, row[0]),
            )
        # 型号表里的输入模态同步改掉（控制台按它显示「支持哪些输入」）。
        db.execute(
            "UPDATE models SET input_modalities_json=?,updated_at=? WHERE code IN ('qwen3.5-omni-plus','qwen3.8-omni-flash')",
            (json.dumps(["text", "image", "video", "audio"]), stamp),
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
        if self.channel(channel_id)["code"] in {"local","jmapi","libtv","grsai","mxapi","runninghub","teamorouter","teamorouter-ads","wuyinkeji","huanwangai","ark"}: raise ValueError("preset channel cannot be deleted; disable it instead")
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

    def create_job(self, model_code:str, requested_channel:str, request:dict[str,Any], *,
                   key_id:str|None=None, idempotency_key:str|None=None)->tuple[str,bool]:
        """新建作业。返回 (job_id, created)；created=False 表示命中了幂等键、返回的是旧作业。

        幂等键缺省为 None（每次都新建）。给了就落到 (api_key_id, idempotency_key) 唯一索引上，
        并发重试时后一条 INSERT 会撞索引，然后回查拿到先建出来的那条。
        """
        models={m["code"]:m for m in self.models()}
        if model_code not in models or not models[model_code]["enabled"]: raise CapabilityNotFound(model_code)
        jid=str(uuid4()); stamp=now()
        owner=key_id or ""
        if idempotency_key:
            with self._db() as db:
                row=db.execute("SELECT id FROM generation_jobs WHERE api_key_id=? AND idempotency_key=?",(owner,idempotency_key)).fetchone()
            if row: return str(row[0]),False
        try:
            with self._db() as db: db.execute("INSERT INTO generation_jobs(id,model_id,requested_channel,status,stage,request_json,api_key_id,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(jid,models[model_code]["id"],requested_channel,"queued","queued",json.dumps(request,ensure_ascii=False),owner,idempotency_key,stamp,stamp))
        except sqlite3.IntegrityError:
            with self._db() as db:
                row=db.execute("SELECT id FROM generation_jobs WHERE api_key_id=? AND idempotency_key=?",(owner,idempotency_key)).fetchone()
            if row is None: raise
            return str(row[0]),False
        return jid,True

    def job_by_idempotency(self,key_id:str|None,idempotency_key:str)->dict[str,Any]|None:
        """按幂等键找作业（幂等键缺省就是请求体里的 external_ref）。只在本 Key 范围内找。"""
        with self._db() as db:
            row=db.execute(
                "SELECT id FROM generation_jobs WHERE (idempotency_key=? OR json_extract(request_json,'$.external_ref')=?) "
                "AND coalesce(api_key_id,'')=? ORDER BY created_at LIMIT 1",
                (idempotency_key,idempotency_key,key_id or ""),
            ).fetchone()
        return self.job(str(row[0])) if row else None

    def update_job(self,jid:str,**values:Any)->None:
        values["updated_at"]=now()
        with self._db() as db: db.execute(f"UPDATE generation_jobs SET {','.join(k+'=?' for k in values)} WHERE id=?",(*values.values(),jid))
        # 跑到终态就把预占的额度结掉。只靠轮询口结算是会漏的：调用方不轮询、或者任务
        # 是被 worker 自己判死/超时的，held 行会一直挂在账上——对「累计总额」窗口就是永久占位。
        status=values.get("status")
        if status in {"succeeded","failed","cancelled"}:
            keys=_api_key_store()
            if keys is not None:
                try: keys.settle_job(jid,str(status))
                except Exception: pass  # 结账失败不能把作业状态更新弄挂

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
        if from_date:
            start=datetime.fromisoformat(f"{from_date}T00:00:00").replace(tzinfo=SHANGHAI)
            clauses.append("j.created_at>=?"); params.append(start.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
        if to_date:
            end=datetime.fromisoformat(f"{to_date}T00:00:00").replace(tzinfo=SHANGHAI)+timedelta(days=1)
            clauses.append("j.created_at<?"); params.append(end.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
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
