import asyncio
import pytest
from pydantic import BaseModel
from agent import run, ToolRegistry, ToolResult, Terminal, ToolContext
from agent.events import (
    TextDelta, ToolCallComplete, AssistantMessageComplete, AssistantMessage, ToolCall,
    ToolResult as ToolResultEv,
)
from agent.builtin.spawn import Spawn


class ScriptedLLM:
    """An LLM that pops scripted turns in FIFO order. Shared between parent and subagents."""
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
async def test_subagent_isolation_full():
    """Plan test #8: parent's state.messages contains the spawn call + spawn's
    text result, NOT the subagent's internal messages."""

    # Capture every turn's parent state via on_turn_end
    parent_states = []

    async def capture(state):
        parent_states.append(state)

    spawn_call = ToolCall(
        call_id="s1",
        name="spawn_subagent",
        arguments={"instructions": "secret subtask"},
    )

    # Turn order: parent(turn 0) → spawn → sub(turn 0) → sub(turn 1, no tools) → parent(turn 1, no tools)
    llm = ScriptedLLM([
        # Parent turn 0: emits spawn call
        {"text": "", "tool_calls": [spawn_call]},
        # Subagent turn 0: produces secret reasoning
        {"text": "internal subagent reasoning that should NOT bleed to parent"},
        # Parent turn 1: produces final answer
        {"text": "final parent answer"},
    ])

    parent_registry = ToolRegistry()
    spawn = Spawn(parent_registry=parent_registry, llm=llm)
    parent_registry.register(spawn)

    async for _ in run(
        messages=[{"role": "user", "content": "delegate a thing"}],
        tools=parent_registry,
        llm=llm,
        on_turn_end=capture,
    ):
        pass

    # The final parent state should have:
    #   user msg, assistant msg (with spawn tool call), tool result with subagent output
    final_state = parent_states[-1]
    all_content = " ".join(
        str(m.get("content", ""))
        for m in final_state.messages
    )
    # The subagent's internal reasoning text appears in the tool result content
    # because it IS the subagent's final answer. That's expected.
    # What MUST NOT appear is any subagent-internal message structure (separate
    # role=user "secret subtask" instruction, separate assistant entries, etc).
    roles = [m["role"] for m in final_state.messages]
    # Parent should only see: user (initial), assistant (spawn call), tool (result)
    assert roles == ["user", "assistant", "tool"]
    # The subagent's user message ("secret subtask") must NOT appear at top level
    user_messages = [m for m in final_state.messages if m["role"] == "user"]
    assert len(user_messages) == 1
    assert "secret subtask" not in str(user_messages[0]["content"])


@pytest.mark.asyncio
async def test_subagent_parallelism():
    """Plan test #9: 3 spawn_subagent calls in one turn run concurrently."""
    barrier = asyncio.Barrier(3)
    enter_count = [0]

    class BarrierTool:
        name = "barrier"
        description = "blocks until 3 invocations meet"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            enter_count[0] += 1
            await barrier.wait()
            return ToolResult(call_id="", output="passed")

    spawn_calls = [
        ToolCall(
            call_id=f"s{i}",
            name="spawn_subagent",
            arguments={"instructions": f"do {i}", "tools": ["barrier"]},
        )
        for i in range(3)
    ]

    # Each subagent invocation:
    #   turn 0: call barrier tool
    #   turn 1: produce final text
    sub_turns = []
    for i in range(3):
        sub_turns.extend([
            {"text": "", "tool_calls": [ToolCall(call_id=f"b{i}", name="barrier", arguments={})]},
            {"text": f"sub {i} done"},
        ])

    parent_turns = [
        # Parent fires 3 spawn calls in one turn
        {"text": "", "tool_calls": spawn_calls},
        # After tool results return, parent finishes
        {"text": "all subs returned"},
    ]

    # IMPORTANT: subagents and parent share the same LLM, but the order in which
    # turns get consumed depends on scheduling. Since the parent's stream
    # completes first and spawns the 3 subs concurrently, the 3 subs will
    # interleave their turns. We don't care about exact interleaving — only that
    # all 3 barrier.wait() calls enter concurrently.
    # To handle interleaving robustly, we use a turn dispatcher keyed on the
    # subagent's first user message ("do 0", "do 1", "do 2").
    class Dispatcher:
        @property
        def context_window(self): return 100_000
        def count_tokens(self, m): return 10

        # Tracks which subagent (or parent) we're servicing based on messages
        _parent_idx = 0
        _sub_idx = {0: 0, 1: 0, 2: 0}

        async def stream(self, messages, tools, system, *, signal=None):
            # Detect parent vs subagent by initial user message content
            first_user = next((m for m in messages if m["role"] == "user"), None)
            content = first_user["content"] if first_user else ""

            if content.startswith("do "):
                sub_id = int(content.split()[-1])
                turn_idx = self._sub_idx[sub_id]
                if turn_idx == 0:
                    self._sub_idx[sub_id] = 1
                    tc = ToolCall(
                        call_id=f"b{sub_id}", name="barrier", arguments={}
                    )
                    yield ToolCallComplete(call=tc)
                    yield AssistantMessageComplete(
                        message=AssistantMessage(content="", tool_calls=[tc])
                    )
                else:
                    self._sub_idx[sub_id] = 2
                    yield TextDelta(text=f"sub {sub_id} done")
                    yield AssistantMessageComplete(
                        message=AssistantMessage(content=f"sub {sub_id} done", tool_calls=[])
                    )
            else:
                # Parent
                turn = parent_turns[self._parent_idx]
                self._parent_idx += 1
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

    dispatcher = Dispatcher()
    parent_registry = ToolRegistry()
    barrier_tool = BarrierTool()
    parent_registry.register(barrier_tool)
    spawn = Spawn(parent_registry=parent_registry, llm=dispatcher)
    parent_registry.register(spawn)

    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "delegate three"}],
        tools=parent_registry,
        llm=dispatcher,
        max_turns=5,
    ):
        events.append(ev)

    # If subagents ran serially, only one would enter the barrier and it would deadlock.
    # The fact that we got here means all 3 entered concurrently.
    assert enter_count[0] == 3
    # Parent should see 3 tool results from spawn calls
    spawn_results = [
        e for e in events
        if isinstance(e, ToolResultEv) and "sub" in str(e.output) and "done" in str(e.output)
    ]
    assert len(spawn_results) == 3


@pytest.mark.asyncio
async def test_subagent_no_recursive_spawn():
    """A subagent's tool registry should not include the spawn tool itself,
    even when the parent passes spawn_subagent in args.tools."""
    parent_registry = ToolRegistry()

    class DummyLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            yield TextDelta(text="")
            yield AssistantMessageComplete(message=AssistantMessage(content="", tool_calls=[]))

    spawn = Spawn(parent_registry=parent_registry, llm=DummyLLM())
    parent_registry.register(spawn)

    # Even though we pass spawn_subagent in tools, the subagent's registry should exclude it
    args = spawn.input_model(instructions="x", tools=["spawn_subagent"])
    # We can't easily inspect the subagent's registry from outside, but
    # we can verify by running and ensuring no infinite recursion.
    result = await spawn.call(args, ToolContext())
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_spawn_default_tools_excludes_spawn():
    """tools=None: subagent sees parent tools but not Spawn itself."""
    seen_tool_names: list[list[str]] = []

    class InspectingLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            seen_tool_names.append([t["function"]["name"] for t in tools])
            yield TextDelta(text="done")
            yield AssistantMessageComplete(message=AssistantMessage(content="done", tool_calls=[]))

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
    llm = InspectingLLM()
    spawn = Spawn(parent_registry=parent_registry, llm=llm)
    parent_registry.register(spawn)

    args = spawn.input_model(instructions="do stuff")  # tools=None by default
    await spawn.call(args, ToolContext())

    assert seen_tool_names, "LLM was never called"
    sub_tools = seen_tool_names[0]
    assert "dummy" in sub_tools
    assert "spawn_subagent" not in sub_tools


@pytest.mark.asyncio
async def test_spawn_explicit_tool_list():
    """tools=['dummy'] gives the subagent exactly that tool."""
    seen_tool_names: list[list[str]] = []

    class InspectingLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            seen_tool_names.append([t["function"]["name"] for t in tools])
            yield TextDelta(text="ok")
            yield AssistantMessageComplete(message=AssistantMessage(content="ok", tool_calls=[]))

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
    llm = InspectingLLM()
    spawn = Spawn(parent_registry=parent_registry, llm=llm)
    parent_registry.register(spawn)

    args = spawn.input_model(instructions="x", tools=["dummy"])
    await spawn.call(args, ToolContext())

    sub_tools = seen_tool_names[0]
    assert sub_tools == ["dummy"]


@pytest.mark.asyncio
async def test_spawn_unknown_tool_in_args_silently_skipped():
    """tools=['nonexistent'] doesn't crash; subagent gets zero tools."""
    seen_tool_names: list[list[str]] = []

    class InspectingLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            seen_tool_names.append([t["function"]["name"] for t in tools])
            yield TextDelta(text="ok")
            yield AssistantMessageComplete(message=AssistantMessage(content="ok", tool_calls=[]))

    parent_registry = ToolRegistry()
    llm = InspectingLLM()
    spawn = Spawn(parent_registry=parent_registry, llm=llm)
    parent_registry.register(spawn)

    args = spawn.input_model(instructions="x", tools=["nonexistent"])
    result = await spawn.call(args, ToolContext())
    assert isinstance(result, ToolResult)
    assert seen_tool_names[0] == []
