import asyncio
import json
import pytest
from slim_agent.state import LoopState
from slim_agent.compact import maybe_compact, COMPACT_THRESHOLD
from slim_agent.events import TextDelta, AssistantMessageComplete, AssistantMessage


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
    from slim_agent.compact import COMPACT_KEEP_TAIL
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
    from slim_agent.compact import COMPACT_KEEP_TAIL
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
