from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


def utc_iso(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(
        tzinfo=SHANGHAI
    ).astimezone(UTC).isoformat()


def local(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(SHANGHAI).isoformat(
        timespec="seconds"
    )


def request_summary(raw: str) -> dict[str, object]:
    try:
        request = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    prompt = request.get("prompt")
    prompts = request.get("prompts")
    if not isinstance(prompt, str) and isinstance(prompts, list):
        prompt = "".join(item for item in prompts if isinstance(item, str))
    return {
        "duration_seconds": request.get("duration_seconds"),
        "resolution": request.get("resolution"),
        "quality": request.get("quality"),
        "aspect_ratio": request.get("aspect_ratio"),
        "priority": request.get("priority"),
        "prompt_characters": len(prompt) if isinstance(prompt, str) else 0,
        "reference_images": len(request.get("reference_image_urls") or []),
        "reference_videos": len(request.get("reference_video_urls") or []),
        "reference_audios": len(request.get("reference_audio_urls") or []),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/donxu/ai-centre"))
    parser.add_argument("--start", required=True, help="Asia/Shanghai YYYY-MM-DD HH:MM")
    parser.add_argument("--end", required=True, help="Asia/Shanghai YYYY-MM-DD HH:MM")
    parser.add_argument("--skip-api-calls", action="store_true")
    args = parser.parse_args()
    start, end = utc_iso(args.start), utc_iso(args.end)
    print(json.dumps({"start_utc": start, "end_utc": end}))

    capability_path = args.root / "runtime/ai-capabilities/capabilities.db"
    with sqlite3.connect(capability_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT j.*, m.code model, c.code channel
            FROM generation_jobs j
            JOIN models m ON m.id=j.model_id
            LEFT JOIN channels c ON c.id=j.channel_id
            WHERE j.created_at>=? AND j.created_at<?
            ORDER BY j.created_at
            """,
            (start, end),
        ).fetchall()
        for row in rows:
            item = dict(row)
            print(
                json.dumps(
                    {
                        "source": "general_video",
                        "id": item["id"],
                        "external_ref": json.loads(item["request_json"]).get(
                            "external_ref"
                        ),
                        "model": item["model"],
                        "requested_channel": item["requested_channel"],
                        "channel": item["channel"],
                        "status": item["status"],
                        "stage": item["stage"],
                        "created_at": local(item["created_at"]),
                        "started_at": local(item["started_at"]),
                        "finished_at": local(item["finished_at"]),
                        "elapsed_seconds": item["elapsed_seconds"],
                        "fallback_count": item["fallback_count"],
                        "error": item["error"],
                        "request": request_summary(item["request_json"]),
                    },
                    ensure_ascii=False,
                )
            )

    h3_path = args.root / "runtime/minimax-h3/h3.db"
    with sqlite3.connect(h3_path) as db:
        db.row_factory = sqlite3.Row
        jobs = db.execute(
            "SELECT * FROM jobs WHERE created_at>=? AND created_at<? ORDER BY created_at",
            (start, end),
        ).fetchall()
        for row in jobs:
            item = dict(row)
            attempts = [
                dict(attempt)
                for attempt in db.execute(
                    """
                    SELECT a.attempt_number,a.status,a.error,a.started_at,a.finished_at,
                           w.name worker_name
                    FROM attempts a LEFT JOIN workers w ON w.id=a.worker_id
                    WHERE a.job_id=? ORDER BY a.attempt_number
                    """,
                    (item["id"],),
                ).fetchall()
            ]
            for attempt in attempts:
                attempt["started_at"] = local(attempt["started_at"])
                attempt["finished_at"] = local(attempt["finished_at"])
            print(
                json.dumps(
                    {
                        "source": "minimax_h3",
                        "id": item["id"],
                        "external_ref": item["external_ref"],
                        "status": item["status"],
                        "stage": item["stage"],
                        "created_at": local(item["created_at"]),
                        "started_at": local(item["started_at"]),
                        "finished_at": local(item["finished_at"]),
                        "elapsed_seconds": item["elapsed_seconds"],
                        "attempt_count": item["attempt_count"],
                        "error": item["error"],
                        "request": request_summary(item["request_json"]),
                        "attempts": attempts,
                    },
                    ensure_ascii=False,
                )
            )

    if args.skip_api_calls:
        return
    observability_path = args.root / "runtime/observability/observability.db"
    with sqlite3.connect(observability_path) as db:
        db.row_factory = sqlite3.Row
        calls = db.execute(
            """
            SELECT trace_id,started_at,path,service,operation,status_code,success,
                   duration_ms,task_id,error_code
            FROM api_calls
            WHERE started_at>=? AND started_at<?
              AND path LIKE '/v1/%video%generation%'
            ORDER BY started_at
            """,
            (start, end),
        ).fetchall()
        for row in calls:
            item = dict(row)
            item["source"] = "api_call"
            item["started_at"] = local(item["started_at"])
            print(json.dumps(item, ensure_ascii=False))


if __name__ == "__main__":
    main()
