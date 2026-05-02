import asyncio
import json
from dataclasses import dataclass
from typing import Protocol, Callable, Awaitable
from pydantic import BaseModel

from .events import ToolCall, ToolResult


class ToolContext:
    """Passed to every tool.call() — extensible without breaking the protocol."""
    def __init__(
        self,
        signal: asyncio.Event | None = None,
        expose_tool_errors: bool = False,
        *,
        parent_session_id: str = "unknown",
        current_task_id: str | None = None,
        depth: int = 0,
        parent_abort_event: asyncio.Event | None = None,
    ):
        self.signal = signal
        self.expose_tool_errors = expose_tool_errors
        self.parent_session_id = parent_session_id
        self.current_task_id = current_task_id
        self.depth = depth
        self.parent_abort_event = parent_abort_event


class Tool(Protocol):
    """Required attrs: name, description, input_model. Required methods: call, is_concurrency_safe."""
    name: str
    description: str
    input_model: type[BaseModel]

    async def call(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...

    def is_concurrency_safe(self, args: BaseModel) -> bool: ...


CanUseToolFn = Callable[[str, dict], Awaitable[bool] | bool]


@dataclass
class _Batch:
    safe: bool
    calls: list[ToolCall]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """Register a tool; replaces any existing tool with the same name."""
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_schema(self) -> list[dict]:
        schemas = []
        for tool in self._tools.values():
            schema = tool.input_model.model_json_schema()
            # Remove title from top-level to keep it clean
            schema.pop("title", None)
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                },
            })
        return schemas


def _partition_calls(calls: list[ToolCall], registry: ToolRegistry) -> list[_Batch]:
    """
    Mirror of toolOrchestration.ts:91-115.
    Read-only (safe) tools batch concurrently; any unsafe tool starts a new
    serial batch. Per-call check: same tool can be safe or unsafe depending on args.
    """
    batches: list[_Batch] = []
    for call in calls:
        tool = registry.get(call.name)
        try:
            args = tool.input_model.model_validate(call.arguments) if tool else None
            safe = tool.is_concurrency_safe(args) if tool and args is not None else False
        except Exception:
            safe = False

        if safe and batches and batches[-1].safe:
            batches[-1].calls.append(call)
        else:
            batches.append(_Batch(safe=safe, calls=[call]))
    return batches


async def _run_one(
    call: ToolCall,
    registry: ToolRegistry,
    ctx: ToolContext,
    can_use_tool: CanUseToolFn | None,
) -> ToolResult:
    tool = registry.get(call.name)
    if tool is None:
        return ToolResult(call_id=call.call_id, output=f"Unknown tool: {call.name}", is_error=True)

    if can_use_tool is not None:
        result = can_use_tool(call.name, call.arguments)
        allowed = await result if asyncio.iscoroutine(result) else result
        if not allowed:
            return ToolResult(call_id=call.call_id, output=f"Tool use denied: {call.name}", is_error=True)

    try:
        args = tool.input_model.model_validate(call.arguments)
        result = await tool.call(args, ctx)
        # Ensure call_id is set from the call if tool returns empty call_id
        if not result.call_id:
            result = ToolResult(call_id=call.call_id, output=result.output, is_error=result.is_error)
        return result
    except Exception as e:
        output = str(e) if ctx.expose_tool_errors else f"{type(e).__name__}: tool failed"
        return ToolResult(call_id=call.call_id, output=output, is_error=True)


TOOL_RESULT_SIZE_LIMIT = 50_000  # bytes


def _maybe_truncate(result: ToolResult) -> ToolResult:
    content = result.output if isinstance(result.output, str) else json.dumps(result.output)
    if len(content.encode()) > TOOL_RESULT_SIZE_LIMIT:
        truncated = f"<truncated length={len(content.encode())} />"
        return ToolResult(call_id=result.call_id, output=truncated, is_error=result.is_error)
    return result


def _is_call_safe(call: ToolCall, registry: ToolRegistry) -> bool:
    tool = registry.get(call.name)
    if tool is None:
        return False
    try:
        args = tool.input_model.model_validate(call.arguments)
        return tool.is_concurrency_safe(args)
    except Exception:
        return False


class StreamingExecutor:
    """
    Manages concurrent + serial tool execution while preserving partition order.

    Design (mirrors claude-code's StreamingToolExecutor + toolOrchestration):
    - `add()` starts a tool eagerly IFF it is concurrency-safe AND no unsafe tool
      has been queued yet. This preserves the streaming win where reads overlap
      with the model stream.
    - Once an unsafe tool arrives, all subsequent calls (safe or not) defer until
      `drain_remaining`, which executes batches in strict partitioned order.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        can_use_tool: CanUseToolFn | None,
        signal: asyncio.Event | None,
        expose_tool_errors: bool = False,
        *,
        parent_session_id: str = "unknown",
        current_task_id: str | None = None,
        depth: int = 0,
        parent_abort_event: asyncio.Event | None = None,
    ):
        self._registry = registry
        self._can_use_tool = can_use_tool
        self._ctx = ToolContext(
            signal=signal,
            expose_tool_errors=expose_tool_errors,
            parent_session_id=parent_session_id,
            current_task_id=current_task_id,
            depth=depth,
            parent_abort_event=parent_abort_event,
        )
        # Each entry: (call, task-or-None). Task is set if started eagerly.
        self._entries: list[tuple[ToolCall, asyncio.Task[ToolResult] | None]] = []
        self._streaming_open = True  # flips False once an unsafe tool is queued

    def _start(self, call: ToolCall) -> asyncio.Task[ToolResult]:
        return asyncio.create_task(
            _run_one(call, self._registry, self._ctx, self._can_use_tool)
        )

    def add(self, call: ToolCall) -> None:
        if self._streaming_open and _is_call_safe(call, self._registry):
            self._entries.append((call, self._start(call)))
        else:
            self._streaming_open = False
            self._entries.append((call, None))

    def started_tasks(self) -> list[asyncio.Task[ToolResult]]:
        """Tasks already running (i.e. eagerly-started safe tools)."""
        return [t for _, t in self._entries if t is not None]

    async def drain_completed(self):
        """Yield results for already-started tasks that have finished."""
        still_pending: list[tuple[ToolCall, asyncio.Task[ToolResult] | None]] = []
        for call, task in self._entries:
            if task is not None and task.done():
                yield _maybe_truncate(task.result())
            else:
                still_pending.append((call, task))
        self._entries = still_pending

    async def _await_or_abort(
        self, task: asyncio.Task[ToolResult]
    ) -> tuple[ToolResult | None, bool]:
        """Await task; if signal fires first, cancel task and return (None, True)."""
        signal = self._ctx.signal
        if signal is None:
            return await task, False
        signal_task = asyncio.create_task(signal.wait())
        done, _ = await asyncio.wait(
            {signal_task, task}, return_when=asyncio.FIRST_COMPLETED
        )
        if signal_task in done:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            return None, True
        signal_task.cancel()
        try:
            await signal_task
        except asyncio.CancelledError:
            pass
        return task.result(), False

    async def drain_remaining(self):
        """Drain everything in partitioned order; honor abort signal between/within batches."""
        calls = [c for c, _ in self._entries]
        started: dict[str, asyncio.Task[ToolResult]] = {
            c.call_id: t for c, t in self._entries if t is not None
        }
        self._entries = []
        signal = self._ctx.signal
        aborted = False

        for batch in _partition_calls(calls, self._registry):
            if aborted or (signal is not None and signal.is_set()):
                aborted = True
                for c in batch.calls:
                    t = started.get(c.call_id)
                    if t is not None and not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    yield ToolResult(call_id=c.call_id, output="Aborted", is_error=True)
                continue

            if batch.safe:
                tasks = [started.get(c.call_id) or self._start(c) for c in batch.calls]
                # Race the whole batch against the signal.
                if signal is not None:
                    async def _gather_all():
                        return await asyncio.gather(*tasks)
                    signal_task = asyncio.create_task(signal.wait())
                    gather_task = asyncio.create_task(_gather_all())
                    done, _ = await asyncio.wait(
                        {signal_task, gather_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if signal_task in done:
                        aborted = True
                        gather_task.cancel()
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                                try:
                                    await t
                                except (asyncio.CancelledError, Exception):
                                    pass
                        for c in batch.calls:
                            yield ToolResult(call_id=c.call_id, output="Aborted", is_error=True)
                        continue
                    signal_task.cancel()
                    try:
                        await signal_task
                    except asyncio.CancelledError:
                        pass
                    results = gather_task.result()
                else:
                    results = await asyncio.gather(*tasks)
                for r in results:
                    yield _maybe_truncate(r)
            else:
                for call in batch.calls:
                    task = started.get(call.call_id) or self._start(call)
                    result, was_aborted = await self._await_or_abort(task)
                    if was_aborted:
                        aborted = True
                        yield ToolResult(call_id=call.call_id, output="Aborted", is_error=True)
                    else:
                        assert result is not None
                        yield _maybe_truncate(result)

    async def drain_with_synthetic_errors(self):
        """Cancel all started tasks; yield synthetic error results for every queued call."""
        for call, task in self._entries:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            yield ToolResult(call_id=call.call_id, output="Aborted", is_error=True)
        self._entries = []

    async def cancel_all(self) -> None:
        """Cancel all in-flight tasks. For use in generator finally blocks."""
        running = [t for _, t in self._entries if t is not None and not t.done()]
        for t in running:
            t.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._entries = []
