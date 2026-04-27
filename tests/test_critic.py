import pytest

from slim_agent.critic import critique, _format
from slim_agent.events import TextDelta


class _FakeLLM:
    """Minimal LLM stub: streams fixed text as TextDelta events."""

    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def stream(self, messages, tools, system, *, signal=None):
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        yield TextDelta(text=self._response)

    def count_tokens(self, messages):
        return 0

    @property
    def context_window(self) -> int:
        return 8192


@pytest.mark.asyncio
async def test_critique_pass_returns_none():
    llm = _FakeLLM("PASS")
    result = await critique(
        [{"role": "user", "content": "2+2?"}, {"role": "assistant", "content": "4"}],
        llm,
    )
    assert result is None


@pytest.mark.asyncio
async def test_critique_pass_with_trailing_text_returns_none():
    llm = _FakeLLM("PASS — looks good.")
    result = await critique(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        llm,
    )
    assert result is None


@pytest.mark.asyncio
async def test_critique_issues_returns_text():
    verdict = "ISSUES:\n- Missing edge case\n- Off-by-one error"
    llm = _FakeLLM(verdict)
    result = await critique(
        [{"role": "user", "content": "fix it"}, {"role": "assistant", "content": "done"}],
        llm,
    )
    assert result == verdict


@pytest.mark.asyncio
async def test_critique_empty_response_returns_none():
    """A blank critic response is treated as PASS (don't trigger pointless revision)."""
    llm = _FakeLLM("   \n  ")
    result = await critique(
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
        llm,
    )
    assert result is None


@pytest.mark.asyncio
async def test_critique_empty_transcript_short_circuits():
    """No user/assistant content → don't call the critic at all."""
    llm = _FakeLLM("ISSUES: should never be returned")
    result = await critique([{"role": "tool", "content": "noise"}], llm)
    assert result is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_critique_uses_no_tools():
    llm = _FakeLLM("PASS")
    await critique(
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
        llm,
    )
    assert llm.calls[0]["tools"] == []


def test_format_skips_tool_messages():
    transcript = _format([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "tool output"},
        {"role": "assistant", "content": "final answer"},
    ])
    assert "[user]" in transcript
    assert "[assistant]" in transcript
    assert "tool output" not in transcript
    assert "final answer" in transcript


def test_format_skips_blank_content():
    transcript = _format([
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "   "},
    ])
    assert transcript == ""
