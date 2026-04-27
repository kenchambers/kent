import asyncio
import json
import pytest
from agent.state import LoopState
from agent.compact import maybe_compact, COMPACT_THRESHOLD
from agent.events import TextDelta, AssistantMessageComplete, AssistantMessage


class FakeLLMForCompact:
    def __init__(self, summary: str = "summary text", context_window: int = 1_000):
        self._summary = summary
        self._context_window = context_window

    @property
    def context_window(self): return self._context_window

    def count_tokens(self, messages) -> int:
        return len(json.dumps(messages)) // 4

    async def stream(self, messages, tools, system, *, signal=None):
        yield TextDelta(text=self._summary)
        yield AssistantMessageComplete(message=AssistantMessage(content=self._summary, tool_calls=[]))


@pytest.mark.asyncio
async def test_no_compact_below_threshold():
    llm = FakeLLMForCompact(context_window=100_000)
    state = LoopState(messages=({"role": "user", "content": "hi"},))
    result = await maybe_compact(state, llm)
    assert result is state  # unchanged


@pytest.mark.asyncio
async def test_compact_triggers_above_threshold():
    # Use a tiny context_window so even small messages exceed threshold
    llm = FakeLLMForCompact(context_window=10, summary="compact summary")
    messages = tuple(
        {"role": "user", "content": f"message {i}"} for i in range(20)
    )
    state = LoopState(messages=messages)
    result = await maybe_compact(state, llm)
    # First message should be the summary
    assert "conversation-summary" in result.messages[0]["content"]
    assert "compact summary" in result.messages[0]["content"]
    # Total messages should be fewer
    assert len(result.messages) < len(messages)


@pytest.mark.asyncio
async def test_compact_preserves_tail():
    from agent.compact import COMPACT_KEEP_TAIL
    llm = FakeLLMForCompact(context_window=10, summary="summary")
    messages = tuple(
        {"role": "user", "content": f"msg{i}"} for i in range(20)
    )
    state = LoopState(messages=messages)
    result = await maybe_compact(state, llm)
    # The last COMPACT_KEEP_TAIL messages should appear verbatim
    tail = messages[-COMPACT_KEEP_TAIL:]
    result_tail = result.messages[-COMPACT_KEEP_TAIL:]
    assert result_tail == tail


@pytest.mark.asyncio
async def test_compact_no_op_when_short_history():
    """Over threshold but few messages (≤ COMPACT_KEEP_TAIL): no summarizer called."""
    from agent.compact import COMPACT_KEEP_TAIL
    summarize_calls = []

    class TrackingLLM(FakeLLMForCompact):
        async def stream(self, messages, tools, system, *, signal=None):
            summarize_calls.append(1)
            yield TextDelta(text="summary")
            yield AssistantMessageComplete(message=AssistantMessage(content="summary", tool_calls=[]))

    llm = TrackingLLM(context_window=1)  # tiny → always over threshold
    messages = tuple({"role": "user", "content": f"m{i}"} for i in range(COMPACT_KEEP_TAIL - 1))
    state = LoopState(messages=messages)
    result = await maybe_compact(state, llm)
    assert result is state
    assert summarize_calls == []


@pytest.mark.asyncio
async def test_compact_signature_drops_system_param():
    """Regression: maybe_compact accepts exactly 2 positional args after cleanup."""
    llm = FakeLLMForCompact(context_window=100_000)
    state = LoopState(messages=({"role": "user", "content": "hi"},))
    result = await maybe_compact(state, llm)
    assert result is state


@pytest.mark.asyncio
async def test_compact_embeds_recalled_memory_when_store_provided():
    """Summary message contains both <conversation-summary> and <recalled-memory>."""
    class StubMemoryStore:
        @property
        def session_id(self): return "stub"
        def record_turn(self, messages, *, session_id): pass
        def wake_up(self): return "octarine is my favorite colour"
        def recall(self, query, k=5): return ""

    llm = FakeLLMForCompact(context_window=10, summary="the summary")
    messages = tuple({"role": "user", "content": f"msg {i}"} for i in range(20))
    state = LoopState(messages=messages)

    result = await maybe_compact(state, llm, memory_store=StubMemoryStore())

    content = result.messages[0]["content"]
    assert "<conversation-summary>" in content
    assert "<recalled-memory>" in content
    assert "octarine" in content


@pytest.mark.asyncio
async def test_compact_no_recalled_memory_block_when_store_absent():
    """Summary message has only <conversation-summary> when no store provided."""
    llm = FakeLLMForCompact(context_window=10, summary="the summary")
    messages = tuple({"role": "user", "content": f"msg {i}"} for i in range(20))
    state = LoopState(messages=messages)

    result = await maybe_compact(state, llm)

    content = result.messages[0]["content"]
    assert "<conversation-summary>" in content
    assert "<recalled-memory>" not in content


@pytest.mark.asyncio
async def test_compact_uses_global_wake_up_not_wing_scoped():
    """Regression: maybe_compact must call wake_up() (global), never wake_up_full().

    Plan invariant: wing-scoped diary content is recoverable via memory_recall_here
    mid-session, but compaction stays global-only to keep summary tokens bounded.
    """
    wake_up_calls = 0
    wake_up_full_calls = 0

    class WingedStubStore:
        @property
        def session_id(self): return "winged-stub"
        @property
        def active_wing(self): return "some-wing"
        def record_turn(self, messages, *, session_id): pass
        def wake_up(self):
            nonlocal wake_up_calls
            wake_up_calls += 1
            return "global L0+L1 priming"
        def wake_up_full(self):
            nonlocal wake_up_full_calls
            wake_up_full_calls += 1
            return "global L0+L1 priming\n\nWING-SCOPED DIARY CONTENT"
        def recall(self, query, k=5): return ""
        def recall_in_wing(self, query, k=5): return ""
        def set_active_wing(self, name): pass
        def write_diary(self, kind, text, *, topic=None): pass

    llm = FakeLLMForCompact(context_window=10, summary="the summary")
    messages = tuple({"role": "user", "content": f"msg {i}"} for i in range(20))
    state = LoopState(messages=messages)

    result = await maybe_compact(state, llm, memory_store=WingedStubStore())

    assert wake_up_calls >= 1, "expected maybe_compact to call wake_up()"
    assert wake_up_full_calls == 0, (
        "maybe_compact must NOT call wake_up_full() — that doubles tokens on every "
        "compaction. Wing-scoped diary belongs at session-start only."
    )
    content = result.messages[0]["content"]
    assert "global L0+L1 priming" in content
    assert "WING-SCOPED DIARY CONTENT" not in content


@pytest.mark.asyncio
async def test_compact_no_recalled_memory_block_when_wake_up_empty():
    """When wake_up() returns empty string, <recalled-memory> is not included."""
    class SilentStore:
        @property
        def session_id(self): return "silent"
        def record_turn(self, messages, *, session_id): pass
        def wake_up(self): return ""
        def recall(self, query, k=5): return ""

    llm = FakeLLMForCompact(context_window=10, summary="summary")
    messages = tuple({"role": "user", "content": f"msg {i}"} for i in range(20))
    state = LoopState(messages=messages)

    result = await maybe_compact(state, llm, memory_store=SilentStore())

    content = result.messages[0]["content"]
    assert "<recalled-memory>" not in content
