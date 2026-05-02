"""Process-global orchestration: BackgroundTask registry + per-session inbox.

Powers non-blocking spawn_subagent / shell_spawn / task_status / task_stop. Workers
run in asyncio.create_task; on completion they push a synthetic <task-notification>
user-message into the parent's inbox, which is drained on the next REPL/Discord turn.
"""
from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass
from typing import Literal
from xml.sax.saxutils import escape

from .state import Message


TaskKind = Literal["agent", "shell"]
TaskStatus = Literal["running", "completed", "failed", "killed"]


@dataclass
class BackgroundTask:
    task_id: str
    kind: TaskKind
    parent_session_id: str
    parent_task_id: str | None
    depth: int
    description: str
    status: TaskStatus
    abort_event: asyncio.Event
    aio_task: asyncio.Task | None
    started_at: float
    ended_at: float | None = None
    result: str | None = None
    error: str | None = None
    output_buffer: collections.deque[str] | None = None  # shells only
    output_size: int = 0  # tracked separately so deque cap is byte-bounded


SHELL_BUFFER_BYTES = 64_000


class BackgroundTaskRegistry:
    """Process-global registry of running/completed background tasks.

    Reuses the asyncio.Event abort + asyncio.create_task patterns established
    in agent/tools.py and agent/gateway/heartbeat.py.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._children: dict[str, set[str]] = {}

    def register(self, task: BackgroundTask) -> None:
        self._tasks[task.task_id] = task
        if task.parent_task_id is not None:
            self._children.setdefault(task.parent_task_id, set()).add(task.task_id)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def all(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def list_for_session(self, parent_session_id: str) -> list[BackgroundTask]:
        # Exact match OR any descendant session prefixed with this id.
        prefix = parent_session_id + ":"
        return [
            t for t in self._tasks.values()
            if t.parent_session_id == parent_session_id
            or t.parent_session_id.startswith(prefix)
        ]

    def list_descendants(self, task_id: str) -> list[BackgroundTask]:
        out: list[BackgroundTask] = []
        stack = list(self._children.get(task_id, set()))
        seen: set[str] = set()
        while stack:
            tid = stack.pop()
            if tid in seen:
                continue
            seen.add(tid)
            t = self._tasks.get(tid)
            if t is not None:
                out.append(t)
            stack.extend(self._children.get(tid, set()))
        return out

    def count_running_for(self, parent_session_id: str) -> int:
        return sum(
            1 for t in self.list_for_session(parent_session_id)
            if t.status == "running"
        )

    def kill(self, task_id: str, *, cascade: bool = True) -> int:
        """Set abort_event on the task (and optionally descendants). Returns count signalled."""
        t = self._tasks.get(task_id)
        if t is None:
            return 0
        signalled = 0
        if not t.abort_event.is_set():
            t.abort_event.set()
            signalled += 1
        if cascade:
            for d in self.list_descendants(task_id):
                if not d.abort_event.is_set():
                    d.abort_event.set()
                    signalled += 1
        return signalled

    def mark_done(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        t = self._tasks.get(task_id)
        if t is None:
            return
        t.status = status
        t.ended_at = time.time()
        if result is not None:
            t.result = result
        if error is not None:
            t.error = error

    def append_output(self, task_id: str, chunk: str) -> None:
        """Append a chunk to a shell task's ring buffer; trims oldest entries past cap."""
        t = self._tasks.get(task_id)
        if t is None or t.output_buffer is None:
            return
        t.output_buffer.append(chunk)
        t.output_size += len(chunk.encode("utf-8", errors="replace"))
        while t.output_size > SHELL_BUFFER_BYTES and len(t.output_buffer) > 1:
            removed = t.output_buffer.popleft()
            t.output_size -= len(removed.encode("utf-8", errors="replace"))

    def drop(self, task_id: str) -> None:
        """Remove a finished task from the registry. Caller is responsible for ordering
        (typically called after the parent's inbox drains the task's notification)."""
        t = self._tasks.pop(task_id, None)
        if t is None:
            return
        # Detach from parent's children set.
        if t.parent_task_id is not None:
            kids = self._children.get(t.parent_task_id)
            if kids is not None:
                kids.discard(task_id)
                if not kids:
                    self._children.pop(t.parent_task_id, None)
        # Drop our own children index entry (descendants are not auto-dropped — they
        # have their own lifecycle and notifications).
        self._children.pop(task_id, None)


REGISTRY = BackgroundTaskRegistry()


@dataclass
class _InboxEntry:
    msg: Message
    task_id: str | None


class Inbox:
    """Per-session synthetic user-message queue.

    Each entry pairs a <task-notification> Message with the task_id that produced
    it, so that drain() can return both the messages to prepend and the task_ids
    the caller should drop from the registry (the self-destruct mechanic).
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[_InboxEntry]] = {}
        self._waiters: dict[str, asyncio.Event] = {}

    def _waiter_for(self, parent_session_id: str) -> asyncio.Event:
        ev = self._waiters.get(parent_session_id)
        if ev is None:
            ev = asyncio.Event()
            self._waiters[parent_session_id] = ev
        return ev

    def push(
        self,
        parent_session_id: str,
        msg: Message,
        *,
        task_id: str | None = None,
    ) -> None:
        self._queues.setdefault(parent_session_id, []).append(
            _InboxEntry(msg=msg, task_id=task_id)
        )
        self._waiter_for(parent_session_id).set()

    def has_pending(self, parent_session_id: str) -> bool:
        q = self._queues.get(parent_session_id)
        return bool(q)

    def drain(self, parent_session_id: str) -> tuple[list[Message], list[str]]:
        q = self._queues.get(parent_session_id, [])
        if not q:
            # Reset waiter so callers don't immediately fire on stale state.
            ev = self._waiters.get(parent_session_id)
            if ev is not None:
                ev.clear()
            return [], []
        msgs = [e.msg for e in q]
        task_ids = [e.task_id for e in q if e.task_id is not None]
        self._queues[parent_session_id] = []
        ev = self._waiters.get(parent_session_id)
        if ev is not None:
            ev.clear()
        return msgs, task_ids

    def peek(self, parent_session_id: str) -> list[Message]:
        return [e.msg for e in self._queues.get(parent_session_id, [])]

    async def wait_for_any(self, parent_session_id: str) -> None:
        if self.has_pending(parent_session_id):
            return
        await self._waiter_for(parent_session_id).wait()


INBOX = Inbox()


def build_notification(task: BackgroundTask) -> Message:
    """Build a <task-notification> user-role message for delivery on next turn.

    Shape matches the claude-code coordinator pattern (task-id, kind, status,
    summary, result, duration_ms) so the LLM can read and act on it.
    """
    duration_ms = 0
    if task.ended_at is not None:
        duration_ms = max(0, int((task.ended_at - task.started_at) * 1000))
    body = (
        "<task-notification>\n"
        f"<task-id>{escape(task.task_id)}</task-id>\n"
        f"<kind>{escape(task.kind)}</kind>\n"
        f"<status>{escape(task.status)}</status>\n"
        f"<summary>{escape(task.description or '')}</summary>\n"
        f"<result>{escape(task.result or task.error or '')}</result>\n"
        f"<duration_ms>{duration_ms}</duration_ms>\n"
        "</task-notification>"
    )
    return {"role": "user", "content": body}


def link_child_abort(parent: asyncio.Event, child: asyncio.Event) -> asyncio.Task:
    """If parent's abort_event fires, set child's. Returns the watcher task so the
    caller can keep a reference (otherwise asyncio may GC the loose task)."""
    async def _watch() -> None:
        try:
            await parent.wait()
            child.set()
        except asyncio.CancelledError:
            pass
    return asyncio.create_task(_watch())


__all__ = [
    "BackgroundTask",
    "BackgroundTaskRegistry",
    "Inbox",
    "REGISTRY",
    "INBOX",
    "TaskKind",
    "TaskStatus",
    "build_notification",
    "link_child_abort",
    "SHELL_BUFFER_BYTES",
]
