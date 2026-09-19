from __future__ import annotations
from typing import Any
from celery.result import AsyncResult
from .config import Settings

TASK_NAME="control_plane.video_generation"
QUEUE_NAME="video_generation"

class GenerationJobClient:
    def __init__(self,settings:Settings,store:Any): self.settings,self.store=settings,store
    def submit(self,data:dict[str,Any])->str:
        jid=self.store.create_job(data["model"],data.get("channel") or "jmapi",data)
        try: self._app().send_task(TASK_NAME,kwargs={"job_id":jid},task_id=jid,queue=QUEUE_NAME)
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
