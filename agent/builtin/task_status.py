"""task_status — list or inspect background tasks (agents + shells).

Without ``task_id`` returns a compact list of every task in the caller's
session. With a specific ``task_id`` returns full state including the tail
of the shell ring buffer.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..events import ToolResult
from ..orchestration import REGISTRY
from ..tools import ToolContext


class TaskStatusArgs(BaseModel):
    task_id: str | None = Field(
        default=None,
        description="Specific task id to inspect. Omit to list all tasks in this session.",
    )
    tail_bytes: int = Field(
        default=4_000,
        ge=0,
        description="For shell tasks, how many bytes of accumulated output to include.",
    )


def _summary(task) -> dict:  # type: ignore[no-untyped-def]
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "status": task.status,
        "description": task.description,
        "depth": task.depth,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "parent_task_id": task.parent_task_id,
    }


class TaskStatus:
    name = "task_status"
    description = (
        "List background tasks in the current session, or inspect one by id. "
        "Without arguments: returns a compact listing. With task_id: returns full state "
        "and (for shell tasks) the last tail_bytes of accumulated output. "
        "Use this to check if a spawned worker has finished and to read incremental "
        "output from a shell_spawn process."
    )
    input_model = TaskStatusArgs

    def is_concurrency_safe(self, args: TaskStatusArgs) -> bool:
        _ = args
        return True

    async def call(self, args: TaskStatusArgs, ctx: ToolContext) -> ToolResult:
        if args.task_id:
            t = REGISTRY.get(args.task_id)
            if t is None:
                return ToolResult(
                    call_id="",
                    output={"error": f"unknown task_id: {args.task_id}"},
                    is_error=True,
                )
            payload: dict = _summary(t)
            payload["result"] = t.result
            payload["error"] = t.error
            if t.kind == "shell" and t.output_buffer is not None:
                joined = "".join(t.output_buffer)
                if args.tail_bytes >= 0 and len(joined) > args.tail_bytes:
                    joined = "...[truncated]\n" + joined[-args.tail_bytes:]
                payload["output_tail"] = joined
                payload["output_total_bytes"] = t.output_size
            return ToolResult(call_id="", output=payload)

        tasks = REGISTRY.list_for_session(ctx.parent_session_id)
        # Sort: running first (newest), then completed (newest)
        tasks.sort(key=lambda t: (t.status != "running", -t.started_at))
        return ToolResult(
            call_id="",
            output={"tasks": [_summary(t) for t in tasks]},
        )


__all__ = ["TaskStatus", "TaskStatusArgs"]
