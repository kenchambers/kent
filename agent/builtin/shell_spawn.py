"""Background shell tool: spawn a long-running subprocess and return immediately.

Captures stdout/stderr into the BackgroundTask's ring buffer (bounded at
``SHELL_BUFFER_BYTES``). The agent reads accumulated output via ``task_status``
and kills the process via ``task_stop``. On exit (or abort) a
``<task-notification>`` is pushed to the parent's inbox.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import secrets
import time

from pydantic import BaseModel, Field

from ..events import ToolResult
from ..orchestration import (
    INBOX,
    REGISTRY,
    BackgroundTask,
    build_notification,
    link_child_abort,
)
from ..tools import ToolContext
from ._executors import ShellExecutor, default_executor

logger = logging.getLogger(__name__)


class ShellSpawnArgs(BaseModel):
    command: str = Field(min_length=1, description="The shell command to run in the background")
    description: str = Field(default="", description="Short label for the task list")
    timeout_s: int | None = Field(
        default=600,
        description="Kill the command after this many seconds; null = run until completion or stopped",
    )


class ShellSpawn:
    """Run a shell command in the background and return immediately.

    Use for long-running processes (servers, builds, watch commands) and for
    commands whose output you want to read incrementally via ``task_status``.
    Use the regular ``shell`` tool for short commands you just want the result of.
    """
    name = "shell_spawn"
    description = (
        "Run a shell command in the background. Returns <spawned id='s-...'> immediately. "
        "Read output incrementally with task_status(task_id='s-...'). Stop with task_stop. "
        "On exit, a <task-notification> arrives on a later turn. Use this for long-running "
        "processes; use the regular shell tool for short commands you want results of inline."
    )
    input_model = ShellSpawnArgs

    def __init__(self, executor: ShellExecutor | None = None):
        self._executor = executor or default_executor()

    def is_concurrency_safe(self, args: ShellSpawnArgs) -> bool:
        _ = args
        # Background spawn returns immediately, so multiple of them parallelize fine.
        return True

    async def call(self, args: ShellSpawnArgs, ctx: ToolContext) -> ToolResult:
        task_id = f"s-{secrets.token_hex(4)}"
        abort = asyncio.Event()
        watcher: asyncio.Task | None = None
        if ctx.parent_abort_event is not None:
            watcher = link_child_abort(ctx.parent_abort_event, abort)

        description = args.description.strip() or args.command[:80]
        argv = self._executor.build_argv(args.command)
        timeout_s = args.timeout_s

        async def _drive_shell() -> None:
            proc: asyncio.subprocess.Process | None = None
            error_msg: str | None = None
            timed_out = False
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                async def _drain(stream: asyncio.StreamReader | None, label: str) -> None:
                    if stream is None:
                        return
                    while True:
                        chunk = await stream.read(4096)
                        if not chunk:
                            return
                        text = chunk.decode("utf-8", errors="replace")
                        if label == "err":
                            REGISTRY.append_output(task_id, f"[stderr] {text}")
                        else:
                            REGISTRY.append_output(task_id, text)

                drainers = [
                    asyncio.create_task(_drain(proc.stdout, "out")),
                    asyncio.create_task(_drain(proc.stderr, "err")),
                ]
                wait_task = asyncio.create_task(proc.wait())
                signal_task = asyncio.create_task(abort.wait())

                pending: set[asyncio.Task] = {wait_task, signal_task}
                try:
                    done, _ = await asyncio.wait(
                        pending,
                        timeout=timeout_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except Exception:
                    done = set()

                if signal_task in done:
                    # Aborted: kill process, drain remaining output briefly.
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
                elif wait_task not in done:
                    # Timeout fired (no signal, no exit).
                    timed_out = True
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass

                if not signal_task.done():
                    signal_task.cancel()
                if not wait_task.done():
                    wait_task.cancel()
                for d in drainers:
                    if not d.done():
                        d.cancel()
                # Best-effort cleanup; suppress noise from cancelled tasks.
                cleanup: list[asyncio.Task] = [*drainers, wait_task, signal_task]
                await asyncio.gather(*cleanup, return_exceptions=True)

            except asyncio.CancelledError:
                if proc is not None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                logger.warning("shell_spawn %s failed", task_id, exc_info=True)
            finally:
                if watcher is not None and not watcher.done():
                    watcher.cancel()

                if abort.is_set():
                    status: str = "killed"
                elif error_msg is not None:
                    status = "failed"
                elif timed_out:
                    status = "failed"
                    error_msg = (error_msg or "") + (
                        f"timed out after {timeout_s}s" if timeout_s is not None else ""
                    )
                else:
                    rc = proc.returncode if proc is not None else None
                    status = "completed" if rc == 0 else "failed"

                # Build a short summary from the tail of the buffer.
                t = REGISTRY.get(task_id)
                tail = ""
                if t is not None and t.output_buffer is not None:
                    tail_chunks = list(t.output_buffer)[-8:]
                    tail = "".join(tail_chunks)[-2_000:]
                summary = tail or (error_msg or "")
                REGISTRY.mark_done(
                    task_id, status=status,  # type: ignore[arg-type]
                    result=summary or None,
                    error=error_msg,
                )
                t = REGISTRY.get(task_id)
                if t is not None:
                    INBOX.push(
                        ctx.parent_session_id,
                        build_notification(t),
                        task_id=task_id,
                    )

        aio_task = asyncio.create_task(_drive_shell())
        REGISTRY.register(BackgroundTask(
            task_id=task_id,
            kind="shell",
            parent_session_id=ctx.parent_session_id,
            parent_task_id=ctx.current_task_id,
            depth=ctx.depth + 1,
            description=description,
            status="running",
            abort_event=abort,
            aio_task=aio_task,
            started_at=time.time(),
            output_buffer=collections.deque(),
        ))
        return ToolResult(
            call_id="",
            output=f"<spawned id='{task_id}'>{description}</spawned>",
        )


__all__ = ["ShellSpawn", "ShellSpawnArgs"]
