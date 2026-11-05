from __future__ import annotations
from contextvars import ContextVar
from typing import Any
from uuid import uuid4
from celery.result import AsyncResult
from .api_keys import ApiKeyStore
from .config import Settings
from .credit_pricing import estimate_request

TASK_NAME="control_plane.video_generation"
QUEUE_NAME="video_generation"

# 由 require_service_token 在鉴权时写入；asyncio.to_thread 会把 context 复制进线程，
# 所以 submit 在 worker 线程里也能读到。
current_api_key_id: ContextVar[str | None] = ContextVar("current_api_key_id", default=None)


def failure_text(job: AsyncResult, fallback: str = "job failed") -> str:
    """Celery 失败作业 → 一句可读的原因。

    优先用异常本身的消息（那里面写着「哪一段、哪个渠道、上游原话」），拿不到才退回
    泛化的说法。压成一行、截断，避免把堆栈塞进接口响应。

    存在的理由：超分那边原先把 `job.state == "FAILURE"` 固定投影成一句
    「video upscale failed」，于是「分辨率超上限」「帧账错配」这些能直接修的问题，
    在调用方看来全是同一句废话。
    """
    info = job.info
    detail = str(info) if info else ""
    detail = " ".join(detail.split())[:600]
    return detail or fallback


class GenerationJobClient:
    # 子类换掉这三个名字就能复用整条提交/查询/取消链路（见 audio_generation_jobs）。
    task_name=TASK_NAME
    queue_name=QUEUE_NAME
    service_name="video_generation"
    def __init__(self,settings:Settings,store:Any):
        self.settings,self.store=settings,store
        self._key_store:ApiKeyStore|None=None

    def _keys(self)->ApiKeyStore:
        if self._key_store is None:
            self._key_store=ApiKeyStore(self.settings.api_keys_db_path)
        return self._key_store

    def submit(self,data:dict[str,Any],service:str|None=None)->str:
        keys=self._keys()
        key_id=current_api_key_id.get()
        model=str(data.get("model") or "")
        channel=data.get("channel") or "jmapi"
        # 图像与视频共用这一个客户端，按 service_name 估价会把图当成 5 秒视频，
        # 所以图像入口显式传 service="image_generation"。
        service_name=service or self.service_name
        # 先预占额度再建作业：超限时一行都不该留下，调用方拿到的是 429 不是半截作业。
        request_id=uuid4().hex
        held=keys.hold(key_id,request_id,service_name,model or None,channel,
                       estimate_request(keys.rates_cached(),service_name,data,model))
        jid=self.store.create_job(model,channel,data)
        keys.link(str(held["charge_id"]),jid)
        try: self._app().send_task(self.task_name,kwargs={"job_id":jid},task_id=jid,queue=self.queue_name)
        except Exception:
            self.store.update_job(jid,status="failed",stage="enqueue_failed",error="unable to enqueue generation job")
            keys.settle(request_id,"failed")
            raise
        return jid
    def status(self,jid:str)->dict[str,Any]:
        job=self.store.job(jid)
        # 轮询是这条链路里最频繁的调用，顺带把终结状态结算掉，不用再往 worker 里加钩子。
        if job.get("status") in {"succeeded","failed","cancelled"}:
            self._keys().settle_job(jid,str(job["status"]))
        return job
    def cancel(self,jid:str)->dict[str,str]:
        job = self.store.job(jid)
        status = str(job.get("status") or "")
        if status in {"succeeded", "failed", "cancelled"}:
            return {"job_id": jid, "status": status}
        self._app().control.revoke(jid,terminate=False)
        if status == "queued":
            self.store.update_job(jid,status="cancelled",stage="cancelled")
            return {"job_id":jid,"status":"cancelled"}
        self.store.update_job(jid,status="cancel_requested",stage="cancel_requested")
        return {"job_id":jid,"status":"cancel_requested"}
    @staticmethod
    def _app():
        from .celery_app import celery_app
        return celery_app
