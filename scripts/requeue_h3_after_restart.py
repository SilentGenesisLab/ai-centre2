#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from control_plane.celery_app import celery_app
from control_plane.config import get_settings
from control_plane.h3_jobs import H3JobClient, QUEUE_NAME, TASK_NAME
from control_plane.h3_scheduler import H3Scheduler
from control_plane.h3_store import H3Store, utc_now
from control_plane.h3_workflow import ComfyH3Client


ACTIVE_STATUSES = ("queued", "running", "cancel_requested")


def _active_jobs(store: H3Store) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with store._connect() as connection:
        rows = connection.execute(
            f"""SELECT id,request_json,priority,status,stage,attempt_count
                FROM jobs WHERE status IN ({placeholders}) AND result_url IS NULL
                ORDER BY priority DESC,created_at ASC,id ASC""",
            ACTIVE_STATUSES,
        ).fetchall()
    return [
        {
            "job_id": row["id"],
            "request": json.loads(row["request_json"]),
            "priority": int(row["priority"]),
            "status": row["status"],
            "stage": row["stage"],
            "attempt_count": int(row["attempt_count"]),
        }
        for row in rows
    ]


def _running_attempts(store: H3Store) -> list[dict[str, Any]]:
    with store._connect() as connection:
        rows = connection.execute(
            """SELECT a.job_id,a.attempt_number,a.lease_version,a.worker_id,a.prompt_ids_json,w.base_url
               FROM attempts a JOIN workers w ON w.id=a.worker_id WHERE a.status='running'"""
        ).fetchall()
    return [dict(row) for row in rows]


def _write_manifest(path: Path, jobs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"created_at": utc_now(), "jobs": jobs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def snapshot_and_cancel(manifest: Path) -> None:
    settings = get_settings()
    store = H3Store(settings.h3_db_path)
    jobs = _active_jobs(store)
    _write_manifest(manifest, jobs)
    cancelled_prompts = 0
    for attempt in _running_attempts(store):
        prompt_ids = json.loads(attempt["prompt_ids_json"] or "[]")
        if prompt_ids:
            try:
                with ComfyH3Client(str(attempt["base_url"]), timeout_seconds=10) as client:
                    client.cancel(str(prompt_ids[-1]))
                cancelled_prompts += 1
            except Exception:
                pass
    for job in jobs:
        store.update_job(
            str(job["job_id"]),
            status="cancel_requested",
            stage="restart_requested",
        )
    print(json.dumps({"saved_jobs": len(jobs), "cancelled_prompts": cancelled_prompts}))


def _rewind_cancelled_attempts(store: H3Store, job_id: str) -> None:
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM attempts WHERE job_id=? AND status='cancelled'",
            (job_id,),
        )
        remaining = connection.execute(
            "SELECT COALESCE(MAX(attempt_number),0) FROM attempts WHERE job_id=?",
            (job_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE jobs SET attempt_count=?,active_attempt=?,updated_at=? WHERE id=?",
            (remaining, remaining, utc_now(), job_id),
        )


def reset_and_enqueue(manifest: Path) -> None:
    settings = get_settings()
    store = H3Store(settings.h3_db_path)
    scheduler = H3Scheduler(settings, store)
    saved = json.loads(manifest.read_text(encoding="utf-8"))["jobs"]
    jobs_by_id = {str(job["job_id"]): job for job in saved}
    for job in _active_jobs(store):
        jobs_by_id[str(job["job_id"])] = job

    with celery_app.connection_or_acquire() as connection:
        purged = connection.channel().queue_purge(QUEUE_NAME)

    for attempt in _running_attempts(store):
        job_id = str(attempt["job_id"])
        attempt_number = int(attempt["attempt_number"])
        lease_value = f"{job_id}:{attempt_number}:{attempt['lease_version']}"
        store.finish_attempt(job_id, attempt_number, "cancelled", "scheduler restart")
        scheduler.release(str(attempt["worker_id"]), lease_value)

    repaired = 0
    with store._connect() as connection:
        repaired = connection.execute(
            """UPDATE jobs SET status='succeeded',stage='completed',progress=100,error=NULL,updated_at=?
               WHERE result_url IS NOT NULL AND status IN ('queued','running','failed','cancel_requested')""",
            (utc_now(),),
        ).rowcount

    enqueued = 0
    for job_id, saved_job in jobs_by_id.items():
        current = store.job(job_id, include_attempts=False)
        if current["result_url"] or current["status"] == "succeeded":
            continue
        _rewind_cancelled_attempts(store, job_id)
        store.update_job(
            job_id,
            status="queued",
            stage="queued_after_restart",
            progress=15,
            current_worker_id=None,
            error=None,
            finished_at=None,
            elapsed_seconds=None,
        )
        priority = int(saved_job.get("priority") or 500)
        celery_app.send_task(
            TASK_NAME,
            task_id=job_id,
            queue=QUEUE_NAME,
            priority=H3JobClient._broker_priority(priority),
        )
        enqueued += 1
    print(json.dumps({"purged_messages": purged, "repaired_jobs": repaired, "enqueued_jobs": enqueued}))


def requeue_one(job_id: str) -> None:
    settings = get_settings()
    store = H3Store(settings.h3_db_path)
    current = store.job(job_id, include_attempts=False)
    if current["status"] == "running" or current["result_url"]:
        raise RuntimeError("job is running or already has a result")
    if any(attempt["job_id"] == job_id for attempt in _running_attempts(store)):
        raise RuntimeError("job still has a running attempt")
    _rewind_cancelled_attempts(store, job_id)
    current = store.job(job_id, include_attempts=False)
    store.update_job(
        job_id,
        status="queued",
        stage="queued_after_restart",
        progress=15,
        current_worker_id=None,
        error=None,
        finished_at=None,
        elapsed_seconds=None,
    )
    priority = int(current.get("priority") or 500)
    celery_app.send_task(
        TASK_NAME,
        task_id=job_id,
        queue=QUEUE_NAME,
        priority=H3JobClient._broker_priority(priority),
    )
    print(json.dumps({"job_id": job_id, "priority": priority, "status": "queued"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("snapshot-cancel", "reset-enqueue", "requeue-one"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.phase == "snapshot-cancel":
        if args.manifest is None:
            parser.error("--manifest is required")
        snapshot_and_cancel(args.manifest)
    elif args.phase == "reset-enqueue":
        if args.manifest is None:
            parser.error("--manifest is required")
        reset_and_enqueue(args.manifest)
    else:
        if not args.job_id:
            parser.error("--job-id is required")
        requeue_one(args.job_id)


if __name__ == "__main__":
    main()
