from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
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
            self._seed(db)

    def _seed(self, db: sqlite3.Connection) -> None:
        stamp = now()
        presets = [
            ("local", "本地部署", "local", "local_h3", "http://127.0.0.1:8320", 1, 10),
            ("jmapi", "jmapi", "third_party", "jmapi", "", 0, 20),
            ("libtv", "libtv", "third_party", "libtv", "", 0, 30),
            ("grsai", "GRSAI", "third_party", "grsai", "https://grsai.dakka.com.cn", 0, 40),
            ("mxapi", "mxapi", "third_party", "mxapi", "https://open.mxapi.org", 0, 50),
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
        models = [
            ("minimax-h3", "MiniMax H3", "video_generation", ["text","image","video","audio"], "video"),
            ("seedance-2.0", "Seedance 2.0", "video_generation", ["text","image","video"], "video"),
            ("seedance-2.5", "Seedance 2.5", "video_generation", ["text","image","video"], "video"),
            ("gpt-image-2", "GPT Image 2", "image_generation", ["text","image"], "image"),
            ("gpt-image-2.5", "GPT Image 2.5", "image_generation", ["text","image"], "image"),
            ("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst", "image_generation", ["text","image"], "image"),
            ("gpt-image-2.5-flare", "GPT Image 2.5 Flare", "image_generation", ["text","image"], "image"),
            ("nano-banana-2", "Nano Banana 2", "image_generation", ["text","image"], "image"),
            # mxapi 的 Suno：一次生成出两首（两个 task），成品是 opus-in-mp4 的 .m4a
            ("suno-v6", "Suno v6 音乐", "audio_generation", ["text"], "audio"),
            ("suno-sound", "Suno 音效", "audio_generation", ["text"], "audio"),
        ]
        schema = {"duration_seconds":{"type":"integer"},"resolution":{"type":"string"},"aspect_ratio":{"type":"string"}}
        for code,name,capability_type,inputs,output in models:
            db.execute("INSERT OR IGNORE INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid4()),name,code,capability_type,json.dumps(inputs),output,json.dumps(schema),1,None,stamp,stamp))
        db.execute("UPDATE models SET capability_type='image_generation',output_modality='image',updated_at=? WHERE code IN ('gpt-image-2','gpt-image-2.5','gpt-image-2.5-sunburst','gpt-image-2.5-flare','nano-banana-2')",(stamp,))
        audio_schema = {"prompt":{"type":"string"},"lyrics":{"type":"string"},"tags":{"type":"string"},"title":{"type":"string"},"instrumental":{"type":"boolean"},"loop":{"type":"boolean"}}
        db.execute("UPDATE models SET parameter_schema_json=?,updated_at=? WHERE code IN ('suno-v6','suno-sound')",(json.dumps(audio_schema),stamp))
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
          ("gpt-image-2","grsai","gpt-image-2","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("gpt-image-2.5","grsai","gpt-image-2.5","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("gpt-image-2.5-sunburst","grsai","gpt-image-2.5-sunburst","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("gpt-image-2.5-flare","grsai","gpt-image-2.5-flare","/v1/draw/completions","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
          ("nano-banana-2","grsai","nano-banana-2","/v1/draw/nano-banana","/v1/draw/result",{"images":9,"videos":0,"audios":0},1,40),
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
        if self.channel(channel_id)["code"] in {"local","jmapi","libtv","grsai","mxapi"}: raise ValueError("preset channel cannot be deleted; disable it instead")
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
        out=dict(row); out["result_urls"]=json.loads(out.pop("result_urls_json") or "[]"); out["upstream_response"]=json.loads(out.pop("upstream_response_json") or "null")
        req=json.loads(out.pop("request_json")); out["request"]=req if include_request else {"prompt":{"present":bool(req.get("prompt")),"characters":len(req.get("prompt",""))},"reference_images":len(req.get("reference_image_urls",[])),"reference_videos":len(req.get("reference_video_urls",[])),"reference_audios":len(req.get("reference_audio_urls",[]))}
        return out

    def list_jobs(self,limit:int=100)->list[dict[str,Any]]:
        with self._db() as db: ids=[r[0] for r in db.execute("SELECT id FROM generation_jobs ORDER BY created_at DESC LIMIT ?",(limit,))]
        return [self.job(i) for i in ids]
