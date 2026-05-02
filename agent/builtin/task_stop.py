"""task_stop — kill any background task (agent or shell), optionally cascading."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..events import ToolResult
from ..orchestration import REGISTRY
from ..tools import ToolContext


class TaskStopArgs(BaseModel):
    task_id: str = Field(description="Task id to stop (t-... agent or s-... shell)")
    cascade: bool = Field(
        default=True,
        description="If True (default), also stops every descendant spawned by this task.",
    )


class TaskStop:
    name = "task_stop"
    description = (
        "Stop a background task by id. Sets the task's abort signal; the running agent loop "
        "or shell process honors it within a couple seconds. If cascade=true (default), every "
        "descendant spawned by this task is also stopped. A <task-notification> with status='killed' "
        "arrives on a later turn for each affected task."
    )
    input_model = TaskStopArgs

    def is_concurrency_safe(self, args: TaskStopArgs) -> bool:
        _ = args
        return False  # mutates state — serialize after any concurrent reads

    async def call(self, args: TaskStopArgs, ctx: ToolContext) -> ToolResult:
        _ = ctx
        t = REGISTRY.get(args.task_id)
        if t is None:
            return ToolResult(
                call_id="",
                output={"error": f"unknown task_id: {args.task_id}"},
                is_error=True,
            )
        if t.status != "running":
            return ToolResult(
                call_id="",
                output={"task_id": args.task_id, "already": t.status},
            )
        signalled = REGISTRY.kill(args.task_id, cascade=args.cascade)
        return ToolResult(
            call_id="",
            output={
                "task_id": args.task_id,
                "signalled": signalled,
                "cascade": args.cascade,
            },
        )


__all__ = ["TaskStop", "TaskStopArgs"]
