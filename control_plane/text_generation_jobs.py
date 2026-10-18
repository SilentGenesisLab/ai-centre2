from __future__ import annotations

from .generation_jobs import GenerationJobClient


TASK_NAME = "control_plane.text_generation"
QUEUE_NAME = "text_generation"


class TextGenerationJobClient(GenerationJobClient):
    """文本生成作业。记录仍落在 capabilities 库的 generation_jobs 表（和视频/生图/音乐/
    定型决策同一张），只是换到自己的队列 —— 它是秒级的同步调用，不该排在长视频任务后面。"""

    task_name = TASK_NAME
    queue_name = QUEUE_NAME
