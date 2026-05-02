"""Non-blocking, recursion-enabled subagent spawn.

When called, registers a BackgroundTask, fires the worker via
``asyncio.create_task``, and returns ``<spawned id='t-...'>...</spawned>``
immediately. On worker termination the result lands in the parent's inbox as a
``<task-notification>`` user-message that the parent sees on its next turn.

The worker uses the *same* system prompt and *same* tool registry as its parent
(spawn_subagent included), so recursion is the LLM's choice — bounded only by
``MAX_SPAWN_DEPTH`` and ``MAX_TASKS_PER_SESSION``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import AssistantMessageComplete, Terminal, ToolResult
from ..orchestration import (
    INBOX,
    REGISTRY,
    BackgroundTask,
    build_notification,
    link_child_abort,
)
from ..tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from ..llm import LLM
    from ..memory.store import MemoryStore

logger = logging.getLogger(__name__)

MAX_SPAWN_DEPTH = int(os.environ.get("KENT_MAX_SPAWN_DEPTH", "5"))
MAX_TASKS_PER_SESSION = int(os.environ.get("KENT_MAX_TASKS_PER_SESSION", "32"))


class SpawnArgs(BaseModel):
    instructions: str
    description: str = ""
    tools: list[str] | None = None  # None → same registry as parent (recursion enabled)


def _filter_tools(parent: ToolRegistry, allowed: list[str]) -> ToolRegistry:
    sub = ToolRegistry()
    for name in allowed:
        t = parent.get(name)
        if t is not None:
            sub.register(t)
    return sub


def _status_from(reason: str) -> str:
    if reason == "completed":
        return "completed"
    if reason == "aborted":
        return "killed"
    return "failed"


class Spawn:
    """Delegate a subtask to a background subagent.

    Returns immediately with ``<spawned id='t-XXXX'>desc</spawned>``. The worker
    runs concurrently; its result arrives on a later turn as a synthetic
    ``<task-notification>`` user-message that the parent's next turn drains
    from its inbox.
    """
    name = "spawn_subagent"
    description = (
        "Spawn a subagent in the background and return immediately. "
        "Use when the user's request has independent subtasks that can run in parallel, "
        "or when a subtask is complex enough that a fresh context window is helpful. "
        "Returns a <spawned id='t-...'>...</spawned> handle right away — the worker's "
        "final result arrives on a later turn as a <task-notification> user message. "
        "Workers can spawn their own children if their tasks decompose further."
    )
    input_model = SpawnArgs

    def __init__(
        self,
        parent_registry: ToolRegistry,
        llm: "LLM",
        memory_store: "MemoryStore | None" = None,
        system_prompt: str | None = None,
    ):
        self.parent_registry = parent_registry
        self.llm = llm
        self.memory_store = memory_store
        self.system_prompt = system_prompt

    def is_concurrency_safe(self, args: SpawnArgs) -> bool:
        _ = args
        # Spawn returns immediately; multiple spawn calls in one turn parallelize trivially.
        return True

    async def call(self, args: SpawnArgs, ctx: ToolContext) -> ToolResult:
        if ctx.depth >= MAX_SPAWN_DEPTH:
            return ToolResult(
                call_id="",
                output=(
                    f"<error>spawn rejected: depth cap {MAX_SPAWN_DEPTH} reached. "
                    "Do this work directly instead of spawning further.</error>"
                ),
                is_error=True,
            )
        if REGISTRY.count_running_for(ctx.parent_session_id) >= MAX_TASKS_PER_SESSION:
            return ToolResult(
                call_id="",
                output=(
                    f"<error>spawn rejected: {MAX_TASKS_PER_SESSION} tasks already "
                    "running in this session.</error>"
                ),
                is_error=True,
            )

        task_id = f"t-{secrets.token_hex(4)}"
        sub_session_id = f"{ctx.parent_session_id}:{task_id}"

        # Per-session memory store fork. Falls back to the parent store if fork
        # is not available so older test harnesses keep working.
        from ..memory.store import MemoryStore as _MemoryStore  # for typing only
        from typing import cast as _cast

        sub_store: "_MemoryStore | None" = self.memory_store
        fork = getattr(self.memory_store, "fork", None)
        if callable(fork):
            try:
                sub_store = _cast("_MemoryStore", fork(session_id=sub_session_id))
            except Exception:
                logger.warning("spawn: memory_store.fork failed", exc_info=True)
                sub_store = self.memory_store

        sub_tools = (
            self.parent_registry if args.tools is None
            else _filter_tools(self.parent_registry, args.tools)
        )

        abort = asyncio.Event()
        watcher: asyncio.Task | None = None
        if ctx.parent_abort_event is not None:
            watcher = link_child_abort(ctx.parent_abort_event, abort)

        description = args.description.strip() or args.instructions[:80]
        sys_prompt = self.system_prompt
        from ..loop import run as agent_run

        async def _drive_worker() -> None:
            final_text = ""
            error_msg: str | None = None
            terminal_reason = "completed"
            try:
                async for ev in agent_run(
                    messages=[{"role": "user", "content": args.instructions}],
                    tools=sub_tools,
                    llm=self.llm,
                    system=sys_prompt,
                    max_turns=15,
                    signal=abort,
                    memory_store=sub_store,
                    parent_session_id=sub_session_id,
                    current_task_id=task_id,
                    depth=ctx.depth + 1,
                    parent_abort_event=abort,
                ):
                    if isinstance(ev, AssistantMessageComplete):
                        if ev.message.content:
                            final_text = ev.message.content
                    elif isinstance(ev, Terminal):
                        terminal_reason = ev.reason
                        break
            except asyncio.CancelledError:
                terminal_reason = "aborted"
                raise
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                terminal_reason = "failed"
                logger.warning("spawn worker %s failed", task_id, exc_info=True)
            finally:
                if watcher is not None and not watcher.done():
                    watcher.cancel()
                status = _status_from(terminal_reason)
                if abort.is_set() and status != "completed":
                    status = "killed"
                REGISTRY.mark_done(
                    task_id, status=status,  # type: ignore[arg-type]
                    result=final_text or None,
                    error=error_msg,
                )
                t = REGISTRY.get(task_id)
                if t is not None:
                    INBOX.push(
                        ctx.parent_session_id,
                        build_notification(t),
                        task_id=task_id,
                    )

        aio_task = asyncio.create_task(_drive_worker())
        REGISTRY.register(BackgroundTask(
            task_id=task_id,
            kind="agent",
            parent_session_id=ctx.parent_session_id,
            parent_task_id=ctx.current_task_id,
            depth=ctx.depth + 1,
            description=description,
            status="running",
            abort_event=abort,
            aio_task=aio_task,
            started_at=time.time(),
            output_buffer=None,
        ))

        return ToolResult(
            call_id="",
            output=f"<spawned id='{task_id}'>{description}</spawned>",
        )


__all__ = ["Spawn", "SpawnArgs", "MAX_SPAWN_DEPTH", "MAX_TASKS_PER_SESSION"]
