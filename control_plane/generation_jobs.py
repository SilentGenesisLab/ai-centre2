from __future__ import annotations
from typing import Any
from celery.result import AsyncResult
from .config import Settings

TASK_NAME="control_plane.video_generation"
QUEUE_NAME="video_generation"


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
    # 子类换掉这两个名字就能复用整条提交/查询/取消链路（见 audio_generation_jobs）。
    task_name=TASK_NAME
    queue_name=QUEUE_NAME
    def __init__(self,settings:Settings,store:Any): self.settings,self.store=settings,store
    def submit(self,data:dict[str,Any])->str:
        jid=self.store.create_job(data["model"],data.get("channel") or "jmapi",data)
        try: self._app().send_task(self.task_name,kwargs={"job_id":jid},task_id=jid,queue=self.queue_name)
        except Exception:
            self.store.update_job(jid,status="failed",stage="enqueue_failed",error="unable to enqueue generation job")
            raise
        return jid
    def status(self,jid:str)->dict[str,Any]: return self.store.job(jid)
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
