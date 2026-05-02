"""Tests for the non-blocking, recursion-enabled Spawn tool.

Spawn now returns ``<spawned id='t-...'>`` synchronously and runs the worker
in ``asyncio.create_task``. Workers post a ``<task-notification>`` to the
parent's inbox on completion. The parent's next turn drains and drops it.
"""
import asyncio
import re

import pytest
from pydantic import BaseModel

from agent import ToolRegistry, ToolResult, ToolContext
from agent.events import (
    AssistantMessage,
    AssistantMessageComplete,
    TextDelta,
    ToolCall,
    ToolCallComplete,
)
from agent.builtin.spawn import (
    MAX_SPAWN_DEPTH,
    MAX_TASKS_PER_SESSION,
    Spawn,
)
from agent.orchestration import INBOX, REGISTRY


SPAWNED_RE = re.compile(r"<spawned id='(t-[0-9a-f]+)'>")


def _new_session_id() -> str:
    """Each test gets a fresh session id so it doesn't see other tests' tasks."""
    import secrets
    return f"test-{secrets.token_hex(3)}"


@pytest.fixture(autouse=True)
def _reset_registry_inbox():
    """Best-effort reset between tests so registry/inbox state doesn't leak."""
    yield
    # Drop any tasks that finished during the test.
    for t in REGISTRY.all():
        if t.status != "running":
            REGISTRY.drop(t.task_id)


class ScriptedLLM:
    """An LLM that pops scripted turns in FIFO order."""
    def __init__(self, turns):
        self._turns = list(turns)

    @property
    def context_window(self): return 100_000
    def count_tokens(self, messages): return 10

    async def stream(self, messages, tools, system, *, signal=None):
        if not self._turns:
            raise RuntimeError("ScriptedLLM exhausted")
        turn = self._turns.pop(0)
        if turn.get("text"):
            yield TextDelta(text=turn["text"])
        for tc in turn.get("tool_calls", []):
            yield ToolCallComplete(call=tc)
        yield AssistantMessageComplete(
            message=AssistantMessage(
                content=turn.get("text", ""),
                tool_calls=turn.get("tool_calls", []),
            )
        )


@pytest.mark.asyncio
async def test_spawn_returns_immediately_with_handle():
    """spawn.call() returns synchronously with <spawned id='t-...'> while the
    worker runs in the background."""
    sub_llm = ScriptedLLM([{"text": "subagent done"}])
    parent_registry = ToolRegistry()
    spawn = Spawn(parent_registry=parent_registry, llm=sub_llm)
    parent_registry.register(spawn)

    session_id = _new_session_id()
    ctx = ToolContext(parent_session_id=session_id)
    result = await spawn.call(spawn.input_model(instructions="do thing"), ctx)
    assert isinstance(result, ToolResult)
    assert "<spawned id=" in str(result.output)
    match = SPAWNED_RE.search(str(result.output))
    assert match is not None
    task_id = match.group(1)

    # Task should be registered (running) initially.
    t = REGISTRY.get(task_id)
    assert t is not None
    assert t.kind == "agent"
    assert t.parent_session_id == session_id

    # Wait briefly for the worker to finish and post its notification.
    aio_task = t.aio_task
    assert aio_task is not None
    await asyncio.wait_for(aio_task, timeout=2.0)

    # Inbox should now contain a <task-notification> for this task id.
    msgs, drained_ids = INBOX.drain(session_id)
    assert task_id in drained_ids
    assert any("<task-notification>" in m.get("content", "") for m in msgs)
    assert any(task_id in m.get("content", "") for m in msgs)

    # Self-destruct: dropping consumed task ids removes them from the registry.
    for tid in drained_ids:
        REGISTRY.drop(tid)
    assert REGISTRY.get(task_id) is None


@pytest.mark.asyncio
async def test_spawn_depth_cap_rejects():
    """ctx.depth >= MAX_SPAWN_DEPTH → spawn returns an error result, no task registered."""
    parent_registry = ToolRegistry()

    class DummyLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, *args, **kw):
            yield TextDelta(text="")
            yield AssistantMessageComplete(message=AssistantMessage(content="", tool_calls=[]))

    spawn = Spawn(parent_registry=parent_registry, llm=DummyLLM())
    parent_registry.register(spawn)

    ctx = ToolContext(parent_session_id=_new_session_id(), depth=MAX_SPAWN_DEPTH)
    result = await spawn.call(spawn.input_model(instructions="x"), ctx)
    assert result.is_error
    assert "depth" in str(result.output).lower()


@pytest.mark.asyncio
async def test_spawn_count_cap_rejects(monkeypatch):
    """Once MAX_TASKS_PER_SESSION running tasks exist for a session, further spawns reject."""
    parent_registry = ToolRegistry()

    class _Slow:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, *args, **kw):
            # Keep the worker hanging so it stays "running" — but we cap quickly so
            # this won't actually be reached in the test.
            await asyncio.sleep(10)
            yield TextDelta(text="")

    spawn = Spawn(parent_registry=parent_registry, llm=_Slow())
    parent_registry.register(spawn)

    session_id = _new_session_id()
    monkeypatch.setattr("agent.builtin.spawn.MAX_TASKS_PER_SESSION", 1)
    # First spawn succeeds.
    first = await spawn.call(spawn.input_model(instructions="a"), ToolContext(parent_session_id=session_id))
    assert not first.is_error
    # Second spawn should reject because we capped at 1.
    second = await spawn.call(spawn.input_model(instructions="b"), ToolContext(parent_session_id=session_id))
    assert second.is_error
    assert "task" in str(second.output).lower()

    # Cleanup: kill the running first task.
    match = SPAWNED_RE.search(str(first.output))
    assert match is not None
    REGISTRY.kill(match.group(1))
    t = REGISTRY.get(match.group(1))
    if t is not None and t.aio_task is not None:
        try:
            await asyncio.wait_for(t.aio_task, timeout=2.0)
        except asyncio.TimeoutError:
            t.aio_task.cancel()


@pytest.mark.asyncio
async def test_spawn_default_tools_inherits_full_parent_registry():
    """tools=None now means: subagent gets the *full* parent registry, including spawn_subagent.
    This is the recursion-enabled change vs. the previous behavior."""
    seen_tool_names: list[list[str]] = []

    class InspectingLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            seen_tool_names.append([t["function"]["name"] for t in tools])
            yield TextDelta(text="ok")
            yield AssistantMessageComplete(
                message=AssistantMessage(content="ok", tool_calls=[])
            )

    class DummyTool:
        name = "dummy"
        description = "dummy"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            return ToolResult(call_id="", output="dummy")

    parent_registry = ToolRegistry()
    parent_registry.register(DummyTool())
    spawn = Spawn(parent_registry=parent_registry, llm=InspectingLLM())
    parent_registry.register(spawn)

    session_id = _new_session_id()
    result = await spawn.call(
        spawn.input_model(instructions="do stuff"),
        ToolContext(parent_session_id=session_id),
    )
    match = SPAWNED_RE.search(str(result.output))
    assert match is not None
    t = REGISTRY.get(match.group(1))
    assert t is not None and t.aio_task is not None
    await asyncio.wait_for(t.aio_task, timeout=2.0)

    assert seen_tool_names, "LLM was never called"
    sub_tools = seen_tool_names[0]
    assert "dummy" in sub_tools
    # Recursion enabled — the subagent SHOULD see spawn_subagent in its tool kit.
    assert "spawn_subagent" in sub_tools


@pytest.mark.asyncio
async def test_spawn_explicit_tool_list_filters():
    """tools=['dummy'] gives the subagent exactly that tool — no spawn, no others."""
    seen_tool_names: list[list[str]] = []

    class InspectingLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            seen_tool_names.append([t["function"]["name"] for t in tools])
            yield TextDelta(text="ok")
            yield AssistantMessageComplete(
                message=AssistantMessage(content="ok", tool_calls=[])
            )

    class DummyTool:
        name = "dummy"
        description = "dummy"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            return ToolResult(call_id="", output="dummy")

    class OtherTool:
        name = "other"
        description = "other"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            return ToolResult(call_id="", output="other")

    parent_registry = ToolRegistry()
    parent_registry.register(DummyTool())
    parent_registry.register(OtherTool())
    spawn = Spawn(parent_registry=parent_registry, llm=InspectingLLM())
    parent_registry.register(spawn)

    result = await spawn.call(
        spawn.input_model(instructions="x", tools=["dummy"]),
        ToolContext(parent_session_id=_new_session_id()),
    )
    match = SPAWNED_RE.search(str(result.output))
    assert match is not None
    t = REGISTRY.get(match.group(1))
    assert t is not None and t.aio_task is not None
    await asyncio.wait_for(t.aio_task, timeout=2.0)

    assert seen_tool_names[0] == ["dummy"]


@pytest.mark.asyncio
async def test_spawn_unknown_tool_in_args_silently_skipped():
    """tools=['nonexistent'] gives the subagent zero tools."""
    seen_tool_names: list[list[str]] = []

    class InspectingLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            seen_tool_names.append([t["function"]["name"] for t in tools])
            yield TextDelta(text="ok")
            yield AssistantMessageComplete(
                message=AssistantMessage(content="ok", tool_calls=[])
            )

    parent_registry = ToolRegistry()
    spawn = Spawn(parent_registry=parent_registry, llm=InspectingLLM())
    parent_registry.register(spawn)

    result = await spawn.call(
        spawn.input_model(instructions="x", tools=["nonexistent"]),
        ToolContext(parent_session_id=_new_session_id()),
    )
    match = SPAWNED_RE.search(str(result.output))
    assert match is not None
    t = REGISTRY.get(match.group(1))
    assert t is not None and t.aio_task is not None
    await asyncio.wait_for(t.aio_task, timeout=2.0)

    assert seen_tool_names[0] == []


@pytest.mark.asyncio
async def test_inbox_drop_self_destructs_task():
    """After draining a notification and calling REGISTRY.drop, the task is gone."""
    sub_llm = ScriptedLLM([{"text": "leaf done"}])
    parent_registry = ToolRegistry()
    spawn = Spawn(parent_registry=parent_registry, llm=sub_llm)
    parent_registry.register(spawn)

    session_id = _new_session_id()
    result = await spawn.call(
        spawn.input_model(instructions="leaf"),
        ToolContext(parent_session_id=session_id),
    )
    match = SPAWNED_RE.search(str(result.output))
    assert match is not None
    task_id = match.group(1)

    t = REGISTRY.get(task_id)
    assert t is not None and t.aio_task is not None
    await asyncio.wait_for(t.aio_task, timeout=2.0)
    assert t.status == "completed"

    msgs, drained_ids = INBOX.drain(session_id)
    assert task_id in drained_ids
    for tid in drained_ids:
        REGISTRY.drop(tid)
    assert REGISTRY.get(task_id) is None


@pytest.mark.asyncio
async def test_task_stop_cascades_to_descendants():
    """Killing a parent task signals every descendant's abort_event."""
    parent_registry = ToolRegistry()

    class _Hang:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, *a, **k):
            await asyncio.sleep(10)
            yield TextDelta(text="")

    spawn = Spawn(parent_registry=parent_registry, llm=_Hang())
    parent_registry.register(spawn)

    session_id = _new_session_id()
    parent_res = await spawn.call(
        spawn.input_model(instructions="A"),
        ToolContext(parent_session_id=session_id),
    )
    parent_id = SPAWNED_RE.search(str(parent_res.output)).group(1)
    parent_task = REGISTRY.get(parent_id)
    assert parent_task is not None

    # Spawn a child of the parent (simulating recursion). We synthesize the ctx
    # with current_task_id=parent_id so registry knows the lineage.
    child_res = await spawn.call(
        spawn.input_model(instructions="B"),
        ToolContext(
            parent_session_id=session_id,
            current_task_id=parent_id,
            depth=1,
            parent_abort_event=parent_task.abort_event,
        ),
    )
    child_id = SPAWNED_RE.search(str(child_res.output)).group(1)
    child_task = REGISTRY.get(child_id)
    assert child_task is not None

    # Kill parent with cascade=True (default).
    REGISTRY.kill(parent_id)
    assert parent_task.abort_event.is_set()
    # link_child_abort propagates from parent → child via the watcher task,
    # but the registry's own list_descendants index also signals it directly.
    # Either way, give the event loop a tick.
    await asyncio.sleep(0.05)
    assert child_task.abort_event.is_set()

    # Cleanup
    for tid in (parent_id, child_id):
        t = REGISTRY.get(tid)
        if t is not None and t.aio_task is not None:
            try:
                await asyncio.wait_for(t.aio_task, timeout=2.0)
            except asyncio.TimeoutError:
                t.aio_task.cancel()
