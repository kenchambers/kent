import asyncio
import json
from typing import AsyncGenerator, Callable, Awaitable, Any

from .state import LoopState, Message
from .events import (
    TurnStart, ToolCallComplete, AssistantMessageComplete,
    ToolResult, ContextOverflow, ModelError, MaxTurnsReached,
    ToolLoopDetected, Terminal, ToolCall,
)
from .llm import LLM, ContextOverflowError
from .tools import ToolRegistry, StreamingExecutor, CanUseToolFn
from .compact import maybe_compact

TOOL_LOOP_REPEAT_THRESHOLD = 3


def _is_repeating(messages: tuple[Message, ...], current_calls: list[ToolCall]) -> bool:
    """Detect if the same tool calls with identical args have appeared repeatedly."""
    if not current_calls:
        return False

    def calls_key(calls):
        return tuple(sorted((c.name, json.dumps(c.arguments, sort_keys=True)) for c in calls))

    current_key = calls_key(current_calls)
    count = 0
    # Walk backwards through message history looking for assistant messages with tool calls
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            prior_calls = [
                ToolCall(
                    call_id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=json.loads(tc["function"]["arguments"]),
                )
                for tc in msg["tool_calls"]
            ]
            if calls_key(prior_calls) == current_key:
                count += 1
                if count >= TOOL_LOOP_REPEAT_THRESHOLD - 1:
                    return True
            else:
                break
    return False


async def run(
    *,
    messages: list[Message],
    tools: ToolRegistry,
    llm: LLM,
    system: str | None = None,
    max_turns: int = 20,
    can_use_tool: CanUseToolFn | None = None,
    on_turn_end: Callable[[LoopState], Awaitable[None]] | None = None,
    signal: asyncio.Event | None = None,
    expose_tool_errors: bool = False,
) -> AsyncGenerator[Any, None]:
    """
    Stream agent events until a Terminal event.

    Yields: TurnStart, TextDelta, ThinkingDelta, ToolCallStart/Delta/Complete,
            AssistantMessageComplete, ToolResult, ContextOverflow, ModelError,
            MaxTurnsReached, ToolLoopDetected, Terminal.
    Terminal reasons: completed, max_turns, aborted, model_error, context_overflow, tool_loop.
    """
    state = LoopState(messages=tuple(messages))
    executor: StreamingExecutor | None = None

    try:
        while True:
            # Phase 1: pre-flight context shaping
            state = await maybe_compact(state, llm)
            yield TurnStart(turn=state.turn)

            # Phase 2: stream model + start tools as they arrive
            executor = StreamingExecutor(tools, can_use_tool, signal, expose_tool_errors)
            assistant_msg = None
            api_error = None

            try:
                async for ev in llm.stream(
                    list(state.messages), tools.to_openai_schema(), system, signal=signal
                ):
                    yield ev
                    if isinstance(ev, ToolCallComplete):
                        executor.add(ev.call)
                    if isinstance(ev, AssistantMessageComplete):
                        assistant_msg = ev.message

                    async for done in executor.drain_completed():
                        yield done

            except ContextOverflowError as e:
                api_error = e
            except Exception as e:
                yield ModelError(error=e)
                yield Terminal(reason="model_error")
                return

            # Phase 3: withhold-then-recover for context overflow
            if api_error is not None:
                if not state.attempted_compact_recovery:
                    state = await maybe_compact(
                        state.advance(attempted_compact_recovery=True), llm
                    )
                    state = state.advance(last_transition="context_overflow_recovery")
                    continue
                yield ContextOverflow(error=api_error)
                yield Terminal(reason="context_overflow")
                return

            # Abort signal handling
            if signal and signal.is_set():
                async for r in executor.drain_with_synthetic_errors():
                    yield r
                yield Terminal(reason="aborted")
                return

            # Phase 4: terminal check (no tools called)
            tool_calls = assistant_msg.tool_calls if assistant_msg else []
            if not tool_calls:
                if on_turn_end:
                    await on_turn_end(state)
                yield Terminal(reason="completed")
                return

            assert assistant_msg is not None  # guaranteed: tool_calls is non-empty above

            # Phase 5: drain remaining tool execution. The executor honors the
            # abort signal internally, racing it against in-flight tasks.
            results: list[Message] = []
            async for r in executor.drain_remaining():
                yield r
                if isinstance(r, ToolResult):
                    results.append(r.to_message())

            if signal is not None and signal.is_set():
                yield Terminal(reason="aborted")
                return

            # Phase 6: advance turn counter, check max_turns
            next_turn = state.turn + 1
            if next_turn >= max_turns:
                yield MaxTurnsReached(turn=next_turn)
                yield Terminal(reason="max_turns")
                return

            # Phase 7: loop-detection guard (only when not hitting max_turns)
            if _is_repeating(state.messages, tool_calls):
                yield ToolLoopDetected(calls=tool_calls)
                yield Terminal(reason="tool_loop")
                return

            if on_turn_end:
                await on_turn_end(state)

            new_messages = (
                *state.messages,
                assistant_msg.to_openai_dict(),
                *results,
            )
            state = state.advance(
                messages=new_messages,
                turn=next_turn,
                attempted_compact_recovery=False,
                last_transition="next_turn",
            )
    finally:
        if executor is not None:
            await executor.cancel_all()
