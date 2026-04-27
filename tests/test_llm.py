import json
import pytest
from unittest.mock import MagicMock

from slim_agent.llm import OpenAICompatibleLLM, ContextOverflowError
from slim_agent.events import TextDelta, ToolCallStart, ToolCallDelta, ToolCallComplete, AssistantMessageComplete


def _make_mock_client(chunks_or_exc):
    """Build a minimal mock that satisfies: await client.chat.completions.create() as stream."""
    class _Stream:
        def __await__(self):
            async def _ret(): return self
            return _ret().__await__()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self._gen()
        async def _gen(self):
            if isinstance(chunks_or_exc, BaseException):
                raise chunks_or_exc
            for c in chunks_or_exc:
                yield c

    class _Completions:
        def create(self_inner, **kwargs):
            return _Stream()

    client = MagicMock()
    client.chat.completions = _Completions()
    return client


def _chunk(content=None, tool_calls=None, finish_reason=None):
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = content
    chunk.choices[0].delta.tool_calls = tool_calls
    chunk.choices[0].finish_reason = finish_reason
    return chunk


def _tc_delta(idx: int, call_id="", name="", args=""):
    tc = MagicMock()
    tc.index = idx
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = args
    return tc


def _make_bad_request_error(code: str = "context_length_exceeded"):
    import httpx
    from openai import BadRequestError
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    resp = httpx.Response(400, request=req)
    return BadRequestError(f"error: {code}", response=resp, body={"code": code})


def _make_auth_error(msg: str = "Invalid API key containing token"):
    import httpx
    from openai import AuthenticationError
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    resp = httpx.Response(401, request=req)
    return AuthenticationError(msg, response=resp, body=None)


def _llm(chunks_or_exc):
    client = _make_mock_client(chunks_or_exc)
    return OpenAICompatibleLLM("http://test", "test", "gpt-4o", _client=client)


@pytest.mark.asyncio
async def test_stream_yields_text_deltas():
    chunks = [
        _chunk(content="Hello"),
        _chunk(content=" world"),
        _chunk(finish_reason="stop"),
    ]
    llm = _llm(chunks)
    events = []
    async for ev in llm.stream([{"role": "user", "content": "hi"}], [], None):
        events.append(ev)
    text_deltas = [e for e in events if isinstance(e, TextDelta)]
    assert len(text_deltas) == 2
    assert "".join(e.text for e in text_deltas) == "Hello world"
    assert any(isinstance(e, AssistantMessageComplete) for e in events)


@pytest.mark.asyncio
async def test_stream_yields_tool_calls():
    """Tool call spread across multiple chunks emits Start, Delta(s), Complete."""
    chunks = [
        _chunk(tool_calls=[_tc_delta(0, call_id="c1", name="my_tool", args="")]),
        _chunk(tool_calls=[_tc_delta(0, call_id="", name="", args='{"x":')]),
        _chunk(tool_calls=[_tc_delta(0, call_id="", name="", args='1}')]),
        _chunk(finish_reason="tool_calls"),
    ]
    llm = _llm(chunks)
    events = []
    async for ev in llm.stream([{"role": "user", "content": "go"}], [], None):
        events.append(ev)

    assert any(isinstance(e, ToolCallStart) for e in events)
    deltas = [e for e in events if isinstance(e, ToolCallDelta)]
    assert len(deltas) == 2
    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(completes) == 1
    assert completes[0].call.arguments == {"x": 1}


@pytest.mark.asyncio
async def test_stream_handles_malformed_args_json():
    """Malformed JSON args become {"_raw": "..."} instead of crashing."""
    chunks = [
        _chunk(tool_calls=[_tc_delta(0, call_id="c1", name="tool", args="{bad json")]),
        _chunk(finish_reason="tool_calls"),
    ]
    llm = _llm(chunks)
    events = []
    async for ev in llm.stream([{"role": "user", "content": "go"}], [], None):
        events.append(ev)
    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(completes) == 1
    assert "_raw" in completes[0].call.arguments


@pytest.mark.asyncio
async def test_stream_overflow_classified():
    """BadRequestError with overflow code raises ContextOverflowError."""
    err = _make_bad_request_error("context_length_exceeded")
    llm = _llm(err)
    with pytest.raises(ContextOverflowError):
        async for _ in llm.stream([{"role": "user", "content": "hi"}], [], None):
            pass


@pytest.mark.asyncio
async def test_stream_auth_error_not_classified_as_overflow():
    """AuthenticationError with 'token' in message is NOT mis-classified as overflow."""
    from openai import AuthenticationError
    err = _make_auth_error("Invalid API key containing token")
    llm = _llm(err)
    with pytest.raises(AuthenticationError):
        async for _ in llm.stream([{"role": "user", "content": "hi"}], [], None):
            pass


def test_count_tokens_uses_tiktoken_when_available():
    """Known OpenAI model names get token-counted via tiktoken."""
    llm = OpenAICompatibleLLM("http://test", "test", "gpt-4o", _client=MagicMock())
    messages = [{"role": "user", "content": "Hello, how are you doing today?"}]
    count = llm.count_tokens(messages)
    assert count > 0


def test_count_tokens_fallback_for_unknown_model():
    """Unknown model names fall back to byte-estimate without raising."""
    llm = OpenAICompatibleLLM("http://test", "test", "some-ollama-model", _client=MagicMock())
    messages = [{"role": "user", "content": "Hello world"}]
    count = llm.count_tokens(messages)
    assert count > 0
