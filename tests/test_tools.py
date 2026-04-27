import asyncio
import pytest
from pydantic import BaseModel
from slim_agent.tools import ToolRegistry, StreamingExecutor, _partition_calls, ToolContext
from slim_agent.events import ToolCall, ToolResult


class ReadTool:
    name = "read"
    description = "Read a file"
    class Args(BaseModel):
        path: str
    input_model = Args
    def is_concurrency_safe(self, args): return True
    async def call(self, args, ctx):
        return ToolResult(call_id="", output=f"content of {args.path}")


class WriteTool:
    name = "write"
    description = "Write a file"
    class Args(BaseModel):
        path: str
        content: str
    input_model = Args
    def is_concurrency_safe(self, args): return False
    async def call(self, args, ctx):
        return ToolResult(call_id="", output=f"wrote {args.path}")


def make_call(name: str, args: dict, call_id: str) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=args)


def test_partition_safe_calls_batch():
    registry = ToolRegistry()
    registry.register(ReadTool())
    calls = [
        make_call("read", {"path": "a"}, "1"),
        make_call("read", {"path": "b"}, "2"),
        make_call("read", {"path": "c"}, "3"),
    ]
    batches = _partition_calls(calls, registry)
    assert len(batches) == 1
    assert batches[0].safe is True
    assert len(batches[0].calls) == 3


def test_partition_write_serializes():
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    calls = [
        make_call("read", {"path": "a"}, "1"),
        make_call("write", {"path": "b", "content": "x"}, "2"),
        make_call("read", {"path": "c"}, "3"),
    ]
    batches = _partition_calls(calls, registry)
    # read batch, write batch, read batch
    assert len(batches) == 3
    assert batches[0].safe is True
    assert batches[1].safe is False
    assert batches[2].safe is True


@pytest.mark.asyncio
async def test_concurrent_reads():
    enter_times = []
    barrier = asyncio.Barrier(3)

    class BarrierRead:
        name = "barrier_read"
        description = "Barrier read"
        class Args(BaseModel):
            idx: int
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            enter_times.append(asyncio.get_event_loop().time())
            await barrier.wait()
            return ToolResult(call_id="", output=f"read {args.idx}")

    registry = ToolRegistry()
    registry.register(BarrierRead())
    executor = StreamingExecutor(registry, None, None)
    calls = [make_call("barrier_read", {"idx": i}, f"c{i}") for i in range(3)]
    for call in calls:
        executor.add(call)

    results = []
    async for r in executor.drain_remaining():
        results.append(r)

    assert len(results) == 3
    # All three entered within a tight window (concurrent)
    assert max(enter_times) - min(enter_times) < 0.5


@pytest.mark.asyncio
async def test_write_serializes_execution_order():
    """Plan test #4: [read, write, read] must execute as read | write | read,
    not read+read concurrent then write."""
    enter_log: list[tuple[str, float]] = []
    in_flight: set[str] = set()
    overlap_violations: list[str] = []

    class TrackedRead:
        name = "read"
        description = "tracked read"
        class Args(BaseModel):
            path: str
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            in_flight.add("read")
            enter_log.append(("read", asyncio.get_event_loop().time()))
            await asyncio.sleep(0.02)
            if "write" in in_flight:
                overlap_violations.append("read overlapped write")
            in_flight.discard("read")
            return ToolResult(call_id="", output=f"r:{args.path}")

    class TrackedWrite:
        name = "write"
        description = "tracked write"
        class Args(BaseModel):
            path: str
        input_model = Args
        def is_concurrency_safe(self, args): return False
        async def call(self, args, ctx):
            in_flight.add("write")
            enter_log.append(("write", asyncio.get_event_loop().time()))
            await asyncio.sleep(0.02)
            if "read" in in_flight:
                overlap_violations.append("write overlapped read")
            in_flight.discard("write")
            return ToolResult(call_id="", output=f"w:{args.path}")

    registry = ToolRegistry()
    registry.register(TrackedRead())
    registry.register(TrackedWrite())
    executor = StreamingExecutor(registry, None, None)
    calls = [
        make_call("read", {"path": "a"}, "1"),
        make_call("write", {"path": "b"}, "2"),
        make_call("read", {"path": "c"}, "3"),
    ]
    for c in calls:
        executor.add(c)

    results = []
    async for r in executor.drain_remaining():
        results.append(r)

    assert overlap_violations == []
    # Order of yielded results must follow [read, write, read]
    assert results[0].output == "r:a"
    assert results[1].output == "w:b"
    assert results[2].output == "r:c"


@pytest.mark.asyncio
async def test_tool_result_size_cap():
    class BigTool:
        name = "big"
        description = "Returns huge output"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            return ToolResult(call_id="c1", output="x" * 100_000)

    registry = ToolRegistry()
    registry.register(BigTool())
    executor = StreamingExecutor(registry, None, None)
    executor.add(make_call("big", {}, "c1"))
    results = []
    async for r in executor.drain_remaining():
        results.append(r)
    assert "<truncated" in results[0].output


def test_partition_unknown_tool_serializes():
    """A call to an unregistered tool is treated as unsafe (serial batch)."""
    registry = ToolRegistry()
    registry.register(ReadTool())
    calls = [
        make_call("read", {"path": "a"}, "1"),
        make_call("unknown", {}, "2"),
        make_call("read", {"path": "c"}, "3"),
    ]
    batches = _partition_calls(calls, registry)
    # unknown tool should NOT merge into the safe read batch
    assert not any(
        b.safe and any(c.name == "unknown" for c in b.calls)
        for b in batches
    )
    unknown_batches = [b for b in batches if any(c.name == "unknown" for c in b.calls)]
    assert len(unknown_batches) == 1
    assert unknown_batches[0].safe is False


def test_partition_invalid_args_serializes():
    """A call with args that fail validation is treated as unsafe."""
    registry = ToolRegistry()
    registry.register(ReadTool())
    # ReadTool.Args requires 'path'; omitting it should cause validation failure
    calls = [make_call("read", {}, "bad")]
    batches = _partition_calls(calls, registry)
    assert len(batches) == 1
    assert batches[0].safe is False


@pytest.mark.asyncio
async def test_drain_with_synthetic_errors_yields_for_unstarted():
    """Calls added after an unsafe tool are never started; drain should yield synthetic errors."""
    registry = ToolRegistry()
    registry.register(WriteTool())
    registry.register(ReadTool())
    executor = StreamingExecutor(registry, None, None)
    # write is unsafe → flips streaming_open to False
    executor.add(make_call("write", {"path": "a", "content": "x"}, "w1"))
    # subsequent read is queued but never started eagerly
    executor.add(make_call("read", {"path": "b"}, "r1"))

    results = []
    async for r in executor.drain_with_synthetic_errors():
        results.append(r)

    assert len(results) == 2
    assert all(r.is_error for r in results)


@pytest.mark.asyncio
async def test_truncation_with_non_string_output():
    """Tool returning a large dict triggers truncation."""
    import json as _json

    class DictTool:
        name = "dict_tool"
        description = "big dict"
        class Args(BaseModel): pass
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            big = {str(i): "v" * 100 for i in range(600)}
            return ToolResult(call_id="d1", output=_json.dumps(big))

    registry = ToolRegistry()
    registry.register(DictTool())
    executor = StreamingExecutor(registry, None, None)
    executor.add(make_call("dict_tool", {}, "d1"))
    results = []
    async for r in executor.drain_remaining():
        results.append(r)
    assert "<truncated" in results[0].output
