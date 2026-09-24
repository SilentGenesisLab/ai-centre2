from __future__ import annotations

from .generation_jobs import GenerationJobClient


TASK_NAME = "control_plane.audio_generation"
QUEUE_NAME = "audio_generation"


class AudioGenerationJobClient(GenerationJobClient):
    """作业记录仍然落在 capabilities 库的 generation_jobs 表（和视频/生图同一张），
    只是把 Celery 任务换到音乐自己的队列——8 条长视频任务不该把音乐堵在后面。"""

    task_name = TASK_NAME
    queue_name = QUEUE_NAME
