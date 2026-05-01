import json
import asyncio
import logging
from typing import Protocol, AsyncGenerator, Any
from .events import (
    TextDelta, ToolCall, ToolCallStart, ToolCallDelta,
    ToolCallComplete, AssistantMessage, AssistantMessageComplete,
)
from .state import Message


logger = logging.getLogger(__name__)


class ContextOverflowError(Exception):
    pass


def _summarize_role_sequence(messages: list[dict]) -> str:
    """Build a short role-sequence summary for diagnosing 400s from servers
    that don't tell us why the request was rejected (e.g. atlascloud).
    Surfaces orphan tool messages and assistant tool_calls without responses.
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        if role == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id", "")[:6] for tc in (m.get("tool_calls") or [])]
            parts.append(f"asst[tc:{','.join(ids)}]")
        elif role == "tool":
            parts.append(f"tool({(m.get('tool_call_id') or '')[:6]})")
        else:
            parts.append(role)
    return " ".join(parts)


class LLM(Protocol):
    def stream(
        self,
        messages: list[Message],
        tools: list[dict],
        system: str | None,
        *,
        signal: asyncio.Event | None = None,
    ) -> AsyncGenerator[Any, None]: ...

    def count_tokens(self, messages: list[Message]) -> int: ...

    @property
    def context_window(self) -> int: ...


class OpenAICompatibleLLM:
    """
    Drives any OpenAI-compatible endpoint: OpenAI, vLLM, Ollama, llama.cpp,
    Together, Groq, OpenRouter.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        context_window: int = 128_000,
        _client: Any = None,
    ):
        """
        context_window must match the model's actual limit — used for compaction decisions.
        _client is a test-injection hook; omit in production.
        """
        from openai import AsyncOpenAI
        import httpx
        self.client = _client or AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
        )
        self.model = model
        self._context_window = context_window

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, messages: list[Message]) -> int:
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model(self.model)
        except (ImportError, KeyError):
            # Fallback: rough estimate
            return len(json.dumps(messages)) // 4

        count = 0
        for msg in messages:
            count += 4  # per-message overhead
            for _, value in msg.items():
                if isinstance(value, str):
                    count += len(enc.encode(value))
                elif isinstance(value, list):
                    count += len(enc.encode(json.dumps(value)))
        return count

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict],
        system: str | None,
        *,
        signal: asyncio.Event | None = None,
    ) -> AsyncGenerator[Any, None]:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": all_messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            # Track partial tool calls across chunks
            pending_calls: dict[int, dict] = {}
            text_buf = ""

            async with await self.client.chat.completions.create(**kwargs) as stream:
                async for chunk in stream:
                    if signal and signal.is_set():
                        return

                    choice = chunk.choices[0] if chunk.choices else None
                    if choice is None:
                        continue

                    delta = choice.delta

                    # Text content
                    if delta.content:
                        text_buf += delta.content
                        yield TextDelta(text=delta.content)

                    # Tool calls
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in pending_calls:
                                pending_calls[idx] = {
                                    "call_id": tc_delta.id or "",
                                    "name": tc_delta.function.name if tc_delta.function else "",
                                    "args_buf": "",
                                }
                                yield ToolCallStart(
                                    call_id=pending_calls[idx]["call_id"],
                                    name=pending_calls[idx]["name"],
                                )
                            else:
                                if tc_delta.id:
                                    pending_calls[idx]["call_id"] = tc_delta.id
                                if tc_delta.function and tc_delta.function.name:
                                    pending_calls[idx]["name"] = tc_delta.function.name

                            if tc_delta.function and tc_delta.function.arguments:
                                pending_calls[idx]["args_buf"] += tc_delta.function.arguments
                                yield ToolCallDelta(
                                    call_id=pending_calls[idx]["call_id"],
                                    args_json_delta=tc_delta.function.arguments,
                                )

                    if choice.finish_reason in ("tool_calls", "stop", "length"):
                        break

            # Emit completed tool calls
            completed_calls = []
            for info in pending_calls.values():
                try:
                    args = json.loads(info["args_buf"]) if info["args_buf"] else {}
                except json.JSONDecodeError:
                    args = {"_raw": info["args_buf"]}
                tc = ToolCall(call_id=info["call_id"], name=info["name"], arguments=args)
                completed_calls.append(tc)
                yield ToolCallComplete(call=tc)

            yield AssistantMessageComplete(
                message=AssistantMessage(content=text_buf, tool_calls=completed_calls)
            )

        except Exception as e:
            from openai import BadRequestError
            if isinstance(e, BadRequestError):
                body = getattr(e, "body", "")
                body_str = str(body)
                code = getattr(e, "code", None) or ""
                logger.error(
                    "LLM 400 from %s: code=%r body=%s | %d msgs: %s",
                    self.model, code, body_str,
                    len(all_messages), _summarize_role_sequence(all_messages),
                )
                body_lower = body_str.lower()
                if (code in ("context_length_exceeded", "string_above_max_length")
                        or "context length" in body_lower
                        or "maximum context" in body_lower):
                    raise ContextOverflowError(str(e)) from e
            raise
