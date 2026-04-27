import asyncio
import json
import pytest
from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel

from slim_agent import (
    run, LoopState, ToolRegistry, ToolContext, ToolResult,
    TextDelta, ToolCallComplete, AssistantMessageComplete, Terminal,
    MaxTurnsReached, ToolLoopDetected, ContextOverflow, ModelError,
    TurnStart, ToolCall, AssistantMessage,
)
from slim_agent.llm import ContextOverflowError
from slim_agent.events import ToolResult as ToolResultEv


@dataclass
class ScriptedTurn:
    text: str = ""
    tool_calls: list[ToolCall] = None
    raises: Exception | None = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []


class FakeLLM:
    def __init__(self, turns: list[ScriptedTurn], context_window: int = 10_000):
        self._turns = list(turns)
        self._context_window = context_window

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, messages) -> int:
        return len(json.dumps(messages)) // 4

    async def stream(self, messages, tools, system, *, signal=None):
        if not self._turns:
            raise RuntimeError("FakeLLM ran out of scripted turns")
        turn = self._turns.pop(0)
        if turn.raises:
            raise turn.raises
        if turn.text:
            yield TextDelta(text=turn.text)
        for tc in turn.tool_calls:
            yield ToolCallComplete(call=tc)
        yield AssistantMessageComplete(
            message=AssistantMessage(content=turn.text, tool_calls=turn.tool_calls)
        )


class EchoTool:
    name = "echo"
    description = "Echo the input"
    class Args(BaseModel):
        text: str
    input_model = Args
    def is_concurrency_safe(self, args): return True
    async def call(self, args, ctx):
        return ToolResult(call_id="", output=f"echo: {args.text}")


def make_tool_call(name: str, args: dict, call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=args)


@pytest.mark.asyncio
async def test_no_tools_completes():
    llm = FakeLLM([ScriptedTurn(text="Hello!")])
    registry = ToolRegistry()
    events = []
    async for ev in run(messages=[{"role": "user", "content": "hi"}], tools=registry, llm=llm):
        events.append(ev)
    terminals = [e for e in events if isinstance(e, Terminal)]
    assert len(terminals) == 1
    assert terminals[0].reason == "completed"
    turn_starts = [e for e in events if isinstance(e, TurnStart)]
    assert turn_starts[0].turn == 0


@pytest.mark.asyncio
async def test_single_tool_roundtrip():
    tc = make_tool_call("echo", {"text": "world"})
    llm = FakeLLM([
        ScriptedTurn(text="", tool_calls=[tc]),
        ScriptedTurn(text="Done"),
    ])
    registry = ToolRegistry()
    registry.register(EchoTool())
    events = []
    async for ev in run(messages=[{"role": "user", "content": "go"}], tools=registry, llm=llm):
        events.append(ev)
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "completed"
    tool_results = [e for e in events if isinstance(e, ToolResultEv)]
    assert len(tool_results) == 1
    assert "echo: world" in tool_results[0].output


@pytest.mark.asyncio
async def test_max_turns():
    # Each turn emits a tool call so the loop keeps going
    tc = make_tool_call("echo", {"text": "x"})
    turns = [ScriptedTurn(text="", tool_calls=[tc]) for _ in range(10)]
    llm = FakeLLM(turns)
    registry = ToolRegistry()
    registry.register(EchoTool())
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        max_turns=3,
    ):
        events.append(ev)
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "max_turns"
    max_ev = next(e for e in events if isinstance(e, MaxTurnsReached))
    assert max_ev.turn == 3


@pytest.mark.asyncio
async def test_tool_loop_detection():
    tc = make_tool_call("echo", {"text": "same"})
    # Repeat the same call many times
    turns = [ScriptedTurn(text="", tool_calls=[tc]) for _ in range(10)]
    llm = FakeLLM(turns)
    registry = ToolRegistry()
    registry.register(EchoTool())
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        max_turns=20,
    ):
        events.append(ev)
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "tool_loop"
    assert any(isinstance(e, ToolLoopDetected) for e in events)


@pytest.mark.asyncio
async def test_context_overflow_recovery():
    tc = make_tool_call("echo", {"text": "x"})
    llm = FakeLLM([
        ScriptedTurn(raises=ContextOverflowError("context too long")),
        ScriptedTurn(text="Recovered"),
    ])
    registry = ToolRegistry()
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
    ):
        events.append(ev)
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "completed"


@pytest.mark.asyncio
async def test_context_overflow_exhausts():
    llm = FakeLLM([
        ScriptedTurn(raises=ContextOverflowError("overflow 1")),
        ScriptedTurn(raises=ContextOverflowError("overflow 2")),
    ])
    registry = ToolRegistry()
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
    ):
        events.append(ev)
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "context_overflow"
    assert any(isinstance(e, ContextOverflow) for e in events)


@pytest.mark.asyncio
async def test_abort_signal():
    signal = asyncio.Event()

    class SlowTool:
        name = "slow"
        description = "Slow tool"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return False
        async def call(self, args, ctx):
            await asyncio.sleep(100)
            return ToolResult(call_id="", output="done")

    tc = make_tool_call("slow", {})
    llm = FakeLLM([ScriptedTurn(text="", tool_calls=[tc])])
    registry = ToolRegistry()
    registry.register(SlowTool())

    async def set_signal_soon():
        await asyncio.sleep(0.01)
        signal.set()

    events = []
    task = asyncio.create_task(set_signal_soon())
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        signal=signal,
    ):
        events.append(ev)
    await task

    terminal = next((e for e in events if isinstance(e, Terminal)), None)
    assert terminal is not None
    assert terminal.reason == "aborted"
    # The slow tool must have yielded a synthetic-error result so the message
    # log stays well-formed (every tool_use needs a tool_result).
    aborted_results = [
        e for e in events if isinstance(e, ToolResultEv) and e.is_error and "Aborted" in str(e.output)
    ]
    assert len(aborted_results) == 1


@pytest.mark.asyncio
async def test_streaming_tool_starts_during_stream():
    """A safe tool yielded mid-stream should start running BEFORE
    AssistantMessageComplete is emitted (the streaming-execution win)."""
    tool_started = asyncio.Event()
    tool_started_before_msg_complete: list[bool] = []

    class StartTracker:
        name = "tracker"
        description = "records when it started"
        class Args(BaseModel):
            i: int
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            tool_started.set()
            await asyncio.sleep(0.05)
            return ToolResult(call_id="", output=f"done {args.i}")

    class SlowStreamLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 10

        async def stream(self, messages, tools, system, *, signal=None):
            tc = ToolCall(call_id="t1", name="tracker", arguments={"i": 1})
            yield ToolCallComplete(call=tc)
            # Simulate the model still streaming text after emitting the tool call
            await asyncio.sleep(0.02)
            tool_started_before_msg_complete.append(tool_started.is_set())
            yield TextDelta(text="thinking...")
            yield AssistantMessageComplete(
                message=AssistantMessage(content="thinking...", tool_calls=[tc])
            )

        # second turn
        _replied = False

    llm = SlowStreamLLM()
    # Need a 2nd turn that ends — patch stream to support both
    turns_remaining = [True]

    class TwoTurnLLM:
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 10

        async def stream(self, messages, tools, system, *, signal=None):
            if turns_remaining[0]:
                turns_remaining[0] = False
                tc = ToolCall(call_id="t1", name="tracker", arguments={"i": 1})
                yield ToolCallComplete(call=tc)
                await asyncio.sleep(0.02)
                tool_started_before_msg_complete.append(tool_started.is_set())
                yield TextDelta(text="thinking...")
                yield AssistantMessageComplete(
                    message=AssistantMessage(content="thinking...", tool_calls=[tc])
                )
            else:
                yield TextDelta(text="done")
                yield AssistantMessageComplete(
                    message=AssistantMessage(content="done", tool_calls=[])
                )

    registry = ToolRegistry()
    registry.register(StartTracker())
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=TwoTurnLLM(),
        max_turns=5,
    ):
        events.append(ev)

    assert tool_started_before_msg_complete == [True], (
        "Expected tool to have started before AssistantMessageComplete fired"
    )


@pytest.mark.asyncio
async def test_model_error_terminal():
    """Non-overflow exceptions from the LLM emit ModelError + Terminal('model_error')."""
    llm = FakeLLM([ScriptedTurn(raises=RuntimeError("boom"))])
    registry = ToolRegistry()
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "hi"}],
        tools=registry,
        llm=llm,
    ):
        events.append(ev)
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "model_error"
    assert any(isinstance(e, ModelError) for e in events)


@pytest.mark.asyncio
async def test_on_turn_end_called():
    llm = FakeLLM([ScriptedTurn(text="Hi")])
    registry = ToolRegistry()
    called = []
    async def on_end(state): called.append(state.turn)
    async for _ in run(
        messages=[{"role": "user", "content": "hi"}],
        tools=registry,
        llm=llm,
        on_turn_end=on_end,
    ):
        pass
    assert 0 in called


@pytest.mark.asyncio
async def test_generator_cleanup_on_break():
    """Breaking out of run() cancels any in-flight tool tasks via the finally clause."""
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()

    class SlowTool:
        name = "slow"
        description = "slow"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            tool_started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                tool_cancelled.set()
                raise
            return ToolResult(call_id="", output="done")

    tc = make_tool_call("slow", {})

    class WaitingLLM:
        """LLM that waits for the slow tool to actually start before completing."""
        @property
        def context_window(self): return 10_000
        def count_tokens(self, m): return 1
        async def stream(self, messages, tools, system, *, signal=None):
            yield ToolCallComplete(call=tc)
            # The tool was eagerly started; wait for it to reach its await point
            # before yielding AssistantMessageComplete.
            await tool_started.wait()
            yield AssistantMessageComplete(
                message=AssistantMessage(content="", tool_calls=[tc])
            )

    registry = ToolRegistry()
    registry.register(SlowTool())
    gen = run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=WaitingLLM(),
    )
    async for ev in gen:
        if isinstance(ev, AssistantMessageComplete):
            break
    await gen.aclose()
    assert tool_cancelled.is_set()


@pytest.mark.asyncio
async def test_compact_recovery_resets_after_progress():
    """After overflow → compact → retry → tool → next turn: attempted_compact_recovery is False."""
    tc = make_tool_call("echo", {"text": "x"})
    llm = FakeLLM([
        ScriptedTurn(raises=ContextOverflowError("overflow")),
        ScriptedTurn(tool_calls=[tc]),
        ScriptedTurn(text="Done"),
    ])
    registry = ToolRegistry()
    registry.register(EchoTool())
    states = []
    async def capture(state): states.append(state)
    async for _ in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        on_turn_end=capture,
    ):
        pass
    assert len(states) >= 1
    assert states[-1].attempted_compact_recovery is False


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_result():
    """A tool call for an unregistered name yields is_error=True and the loop continues."""
    tc = make_tool_call("ghost", {"x": 1})
    llm = FakeLLM([
        ScriptedTurn(tool_calls=[tc]),
        ScriptedTurn(text="OK"),
    ])
    registry = ToolRegistry()
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
    ):
        events.append(ev)
    error_results = [e for e in events if isinstance(e, ToolResultEv) and e.is_error]
    assert len(error_results) == 1
    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "completed"


@pytest.mark.asyncio
async def test_can_use_tool_denial():
    """can_use_tool returning False yields a denied ToolResult."""
    tc = make_tool_call("echo", {"text": "x"})
    llm = FakeLLM([ScriptedTurn(tool_calls=[tc]), ScriptedTurn(text="Done")])
    registry = ToolRegistry()
    registry.register(EchoTool())
    def deny(name, args): return False
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        can_use_tool=deny,
    ):
        events.append(ev)
    denied = [e for e in events if isinstance(e, ToolResultEv) and e.is_error and "denied" in e.output.lower()]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_can_use_tool_async_callback():
    """can_use_tool can be async; same denial behavior."""
    tc = make_tool_call("echo", {"text": "x"})
    llm = FakeLLM([ScriptedTurn(tool_calls=[tc]), ScriptedTurn(text="Done")])
    registry = ToolRegistry()
    registry.register(EchoTool())
    async def deny(name, args): return False
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        can_use_tool=deny,
    ):
        events.append(ev)
    denied = [e for e in events if isinstance(e, ToolResultEv) and e.is_error and "denied" in e.output.lower()]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_tool_error_sanitized_by_default():
    """By default, exception messages from tools are replaced with a generic string."""
    class BrokenTool:
        name = "broken"
        description = "breaks"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            raise ValueError("secret-path/credentials.json")

    tc = make_tool_call("broken", {})
    llm = FakeLLM([ScriptedTurn(tool_calls=[tc]), ScriptedTurn(text="Done")])
    registry = ToolRegistry()
    registry.register(BrokenTool())
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
    ):
        events.append(ev)
    error_results = [e for e in events if isinstance(e, ToolResultEv) and e.is_error]
    assert len(error_results) == 1
    assert "secret-path" not in error_results[0].output


@pytest.mark.asyncio
async def test_tool_error_exposed_with_flag():
    """With expose_tool_errors=True, the raw exception message is returned."""
    class BrokenTool:
        name = "broken"
        description = "breaks"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            raise ValueError("secret-path/credentials.json")

    tc = make_tool_call("broken", {})
    llm = FakeLLM([ScriptedTurn(tool_calls=[tc]), ScriptedTurn(text="Done")])
    registry = ToolRegistry()
    registry.register(BrokenTool())
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm,
        expose_tool_errors=True,
    ):
        events.append(ev)
    error_results = [e for e in events if isinstance(e, ToolResultEv) and e.is_error]
    assert len(error_results) == 1
    assert "secret-path" in error_results[0].output
