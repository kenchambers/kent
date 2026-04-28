from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext

logger = logging.getLogger(__name__)


class TaskStart:
    """Mark the start of a training task rollout."""

    name = "task_start"
    description = "Mark the start of a task. Call at the beginning of each assigned task."

    class Args(BaseModel):
        task_id: str
        prompt: str

    input_model = Args

    def is_concurrency_safe(self, _args: Args) -> bool:
        return True

    async def call(self, args: Args, _ctx: "ToolContext") -> ToolResult:
        logger.info("task_start: task_id=%s", args.task_id)
        return ToolResult(call_id="", output=f"Task {args.task_id!r} started.")


class TaskEnd:
    """Mark the end of a training task rollout."""

    name = "task_end"
    description = "Mark the end of a task. Call when the task is complete."

    class Args(BaseModel):
        task_id: str
        outcome: str

    input_model = Args

    def is_concurrency_safe(self, _args: Args) -> bool:
        return True

    async def call(self, args: Args, _ctx: "ToolContext") -> ToolResult:
        logger.info("task_end: task_id=%s outcome=%s", args.task_id, args.outcome)
        return ToolResult(call_id="", output=f"Task {args.task_id!r} ended: {args.outcome}")
