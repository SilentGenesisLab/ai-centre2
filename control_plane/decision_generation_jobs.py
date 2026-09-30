from __future__ import annotations

from .generation_jobs import GenerationJobClient


TASK_NAME = "control_plane.decision_generation"
QUEUE_NAME = "decision_generation"


class DecisionGenerationJobClient(GenerationJobClient):
    """定型决策作业。记录仍落在 capabilities 库的 generation_jobs 表（和视频/生图/音乐同一张），
    只是把 Celery 任务换到自己的队列——它是同步的短调用，不该排在 8 条长视频任务后面。"""

    task_name = TASK_NAME
    queue_name = QUEUE_NAME
