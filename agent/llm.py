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

        import httpx
        max_retries = 3

        for attempt in range(max_retries + 1):
            if attempt > 0 and signal and signal.is_set():
                return

            try:
                # Build the response
                async with await self.client.chat.completions.create(**kwargs) as stream:
                    # Process all chunks
                    pending_calls: dict[int, dict] = {}
                    text_buf = ""

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

                # Emit completed tool calls (outside the async-with for the stream)
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
                return  # Success — exit the retry loop

            except ContextOverflowError:
                raise  # Never retry context overflow

            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as e:
                if attempt < max_retries:
                    wait = min(1.0 * (2 ** attempt), 10.0)
                    logger.warning("LLM request failed (attempt %d/%d): %s — retrying in %.1fs",
                                   attempt + 1, max_retries, type(e).__name__, wait)
                    await asyncio.sleep(wait)
                    continue
                raise

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < max_retries:
                    wait = min(1.0 * (2 ** attempt), 10.0)
                    logger.warning("LLM returned %d (attempt %d/%d) — retrying in %.1fs",
                                   e.response.status_code, attempt + 1, max_retries, wait)
                    await asyncio.sleep(wait)
                    continue
                raise

            except Exception as e:
                # Check for context length errors before giving up
                from openai import BadRequestError
                if isinstance(e, BadRequestError):
                    code = getattr(e, "code", None) or ""
                    body_str = str(getattr(e, "body", "")).lower()
                    if (code in ("context_length_exceeded", "string_above_max_length")
                            or "context length" in body_str
                            or "maximum context" in body_str):
                        raise ContextOverflowError(str(e)) from e

                # All other exceptions (non-network) — no retry, just propagate
                raise


class FallbackLLM:
    """
    Wraps a primary LLM with a fallback. If the primary errors *before* yielding
    any events, the fallback runs the same request from scratch. Failures after
    streaming has begun propagate — switching mid-stream would duplicate output.
    ContextOverflowError always propagates (the fallback would hit it too).
    """

    def __init__(self, primary: "LLM", fallback: "LLM"):
        self.primary = primary
        self.fallback = fallback

    @property
    def context_window(self) -> int:
        return min(self.primary.context_window, self.fallback.context_window)

    def count_tokens(self, messages: list[Message]) -> int:
        return self.primary.count_tokens(messages)

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict],
        system: str | None,
        *,
        signal: asyncio.Event | None = None,
    ) -> AsyncGenerator[Any, None]:
        yielded = False
        primary_error: Exception | None = None
        try:
            async for event in self.primary.stream(messages, tools, system, signal=signal):
                yielded = True
                yield event
            return
        except (ContextOverflowError, asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as e:
            if yielded:
                raise
            primary_error = e

        logger.warning(
            "primary LLM (%s) failed (%s: %s) — falling back to %s",
            getattr(self.primary, "model", "?"),
            type(primary_error).__name__, primary_error,
            getattr(self.fallback, "model", "?"),
        )
        async for event in self.fallback.stream(messages, tools, system, signal=signal):
            yield event
