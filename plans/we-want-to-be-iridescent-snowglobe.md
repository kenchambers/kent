# Slim Agent — MVP-Ready Hardening

## Context

`agent` is the green-field Python agent runtime built from the `given-our-understanding-now-validated-origami.md` plan. Implementation is complete; 21 tests pass on Python 3.13 in the UV venv. During audit, three categories of gaps surfaced:

1. **Reliability gaps** — early-exit task leaks, brittle string-match error classification, raw `str(e)` leaking in tool error paths.
2. **Documentation gaps** — README is a 2-line stub; no consumer-visible docstrings; no `py.typed` marker so consumers can't see the typed surface.
3. **Small dead code / API rough edges** — unused `system` param in `maybe_compact`, `DEFAULT_SUB_TOOLS=[]` defaulting to "no tools" instead of the plan's stated "read-only default", args validated twice (in partition + in `_run_one`).

Per the user's scoping decisions: **skip timeouts** (signal-based cancellation is enough for v1), **full test coverage on new and previously-untested paths**, **defer CI and Pyright generic refactor** (no team yet, type warnings are cosmetic).

This plan closes those gaps without overengineering.

---

## Recommended approach

### 1. Reliability fixes

#### 1a. Generator cleanup on early exit (`agent/loop.py`)
**Problem**: if the consumer breaks out of `async for ev in run(...)` early (or the loop is cancelled mid-stream), in-flight tool tasks inside `StreamingExecutor` leak. They keep running until they complete, then their `Task` is GC'd with an unawaited result warning.

**Fix**: wrap the body of `run()` in `try / finally`. In `finally`, call `executor.drain_with_synthetic_errors()` and discard results — this cancels every started task and clears the entry list.

```python
async def run(...):
    state = LoopState(messages=tuple(messages))
    executor: StreamingExecutor | None = None
    try:
        while True:
            ...  # existing body, assigning executor each iteration
    finally:
        if executor is not None:
            async for _ in executor.drain_with_synthetic_errors():
                pass
```

Note: `executor` is created fresh per iteration today. Move it to a function-scoped variable so the `finally` clause has a handle to whatever's current.

#### 1b. SDK-typed context-overflow detection (`agent/llm.py`)
**Problem**: `OpenAICompatibleLLM.stream` classifies overflow with `if "context" in err_str or "length" in err_str or "token" in err_str`. Auth errors, rate-limit errors, anything containing "token" can be mis-classified as overflow → trigger a useless compact-and-retry.

**Fix**: catch `openai.BadRequestError` specifically and inspect `e.code` / `e.body` for known overflow markers (`"context_length_exceeded"`, `"string_above_max_length"`, status 400 with body containing `maximum context length`). Everything else propagates as-is and the loop emits `Terminal("model_error")`. Keep a string-match fallback for OSS endpoints that don't return structured codes, but only inside the `BadRequestError` branch (not all exceptions).

```python
from openai import BadRequestError

except BadRequestError as e:
    code = getattr(e, "code", None) or ""
    body_str = str(getattr(e, "body", "")).lower()
    if code in ("context_length_exceeded", "string_above_max_length") \
       or "context length" in body_str or "maximum context" in body_str:
        raise ContextOverflowError(str(e)) from e
    raise
```

#### 1c. Sanitized tool error messages (`agent/tools.py`)
**Problem**: `_run_one` exception path returns `ToolResult(output=str(e))`. `str(e)` from a tool may include filesystem paths, SQL queries, secrets in stack traces. The output goes back to the LLM as a tool result, then into model context, then potentially back to other tools.

**Fix**: by default return a generic `f"{type(e).__name__}: tool failed"`. Add an `expose_tool_errors: bool = False` parameter to `run()` (and thread through `ToolContext`) for debug mode. Tool authors can still raise messages they consider safe — `Tool.call` returning `ToolResult(output=..., is_error=True)` is unaffected.

### 2. Documentation

#### 2a. README rewrite (`README.md`)
Replace the stub with:
- One-paragraph "what / why" (~3 sentences)
- Install: `uv add agent`
- Minimal example: 20-line script that defines an `EchoTool`, builds an `OpenAICompatibleLLM` against Ollama, and prints text deltas
- Tool authoring section: ~10 lines showing the Protocol surface (`name`, `description`, `input_model`, `call`, `is_concurrency_safe`)
- Subagent example: 10 lines registering `Spawn`
- Event reference: bullet list of every event class from `events.py`
- Cancellation: how to use `signal: asyncio.Event`
- Known-not-supported list (so users don't ask): no built-in retries, no rate limiting, no timeouts (use signal), no Anthropic-native API (use a litellm-proxy)

#### 2b. Public-surface docstrings
Add concise docstrings to:
- `agent.run` — args, yielded events, terminal reasons
- `OpenAICompatibleLLM.__init__` — note `context_window` must match the model
- `Tool` Protocol — three required attrs + two methods
- `ToolRegistry.register` — replaces existing tool with same name
- `Spawn` — when/why to use, what sub-tools default to
- `LoopState` — what consumers should and shouldn't touch (read-only)

Keep docstrings ≤ 5 lines each. No examples in docstrings (those go in README).

#### 2c. `py.typed` marker (`agent/py.typed`)
Empty file. Add to `[tool.hatch.build.targets.wheel]` package data so it ships in the wheel:
```toml
[tool.hatch.build.targets.wheel]
packages = ["agent"]
[tool.hatch.build.targets.wheel.force-include]
"agent/py.typed" = "agent/py.typed"
```

### 3. Small cleanups

#### 3a. Drop dead `system` param (`agent/compact.py`)
`maybe_compact(state, llm, system)` accepts `system` only for API consistency, then uses `_ = system` to silence Pyright. Remove the parameter entirely. Update the two callers in `loop.py` (lines 63 and 95) to drop the third arg.

#### 3b. `DEFAULT_SUB_TOOLS=None` semantics (`agent/builtin/spawn.py`)
Currently `DEFAULT_SUB_TOOLS: list[str] = []` and an empty `args.tools` produces a sub-agent with **no** tools. The plan said "empty = read-only default."

**Fix**: change the SpawnArgs default to `tools: list[str] | None = None`. In `Spawn.call`:
- If `args.tools is None`: register every parent tool whose `is_concurrency_safe(default_args)` returns True, except `Spawn` itself. This gives a sane "read-only" default. Tools that take required args will fail the default-arg construction; skip them silently.
- If `args.tools == []`: explicit empty — no tools.
- If `args.tools` is non-empty: register exactly those, skipping `Spawn` itself.

Implementation: don't try to construct default args. Instead, inspect the JSON Schema's `required` list — only include tools whose schema has no required params AND `is_concurrency_safe` defaults to True. Edge case: most tools require args. Document this and accept that the default may be a small set.

Alternative if the above is too clever: keep `args.tools is None` meaning "all parent tools except Spawn", let `is_concurrency_safe` and the model figure it out. **Pick this simpler version** unless tests reveal a concrete problem.

#### 3c. Deduplicate args validation (`agent/tools.py`)
`_partition_calls` validates args via `tool.input_model.model_validate(call.arguments)` then discards them; `_run_one` re-validates. For each call, validation runs twice.

**Fix**: cache validated args on the `_Batch` (or pass parsed args alongside the call). Smallest change: in `_partition_calls`, build a list of `(call, parsed_args | None)` tuples. Pass parsed args into `_run_one` as an optional pre-parsed argument. If `None`, `_run_one` parses (handles the eager-start path where partition hasn't run).

This is a microoptimization — if it complicates code, **skip it**. Pyright will still flag it as wasteful but it's <1ms per call.

### 4. Full test coverage

Add tests for every previously-untested code path. New test files are not needed; extend existing ones.

#### 4a. `tests/test_loop.py` additions
- `test_generator_cleanup_on_break` — start a slow tool, break out of `async for ev in run(...)`. Assert the slow task is cancelled (use a tracking flag set in tool's `except CancelledError`).
- `test_compact_recovery_resets_after_progress` — overflow on turn 0, recover, then turn 1 succeeds. Assert `state.attempted_compact_recovery` is False at turn 1's start.
- `test_unknown_tool_returns_error_result` — model emits a tool call for a name not in registry. Assert `ToolResult(is_error=True)` with sanitized message; loop continues to next turn.
- `test_can_use_tool_denial` — `can_use_tool` returns False. Assert `ToolResult(is_error=True, output="Tool use denied: ...")`.
- `test_can_use_tool_async_callback` — `can_use_tool` is `async def`. Same assertion.
- `test_tool_error_sanitized_by_default` — tool raises `ValueError("secret-path/credentials.json")`. Assert returned `ToolResult.output` does NOT contain "secret-path".
- `test_tool_error_exposed_with_flag` — same setup, but `expose_tool_errors=True`. Assert message DOES contain "secret-path".

#### 4b. `tests/test_llm.py` (new file, ~150 LOC)
Unit tests for `OpenAICompatibleLLM.stream` using a mocked `AsyncOpenAI` client.

Inject the mock by accepting a pre-built client in `__init__`:
```python
def __init__(self, ..., _client: AsyncOpenAI | None = None):
    self.client = _client or AsyncOpenAI(base_url=..., api_key=...)
```

Tests:
- `test_stream_yields_text_deltas` — mock client yields chunks with `delta.content`. Assert `TextDelta` events.
- `test_stream_yields_tool_calls` — mock client yields tool-call chunks across multiple deltas (function name in chunk 1, args in chunks 2-3, finish in chunk 4). Assert one `ToolCallStart`, multiple `ToolCallDelta`, one `ToolCallComplete` with parsed args.
- `test_stream_handles_malformed_args_json` — args buffer doesn't parse as JSON. Assert `ToolCallComplete` with `arguments={"_raw": "..."}` instead of crashing.
- `test_stream_overflow_classified` — mock raises `openai.BadRequestError(body={"code": "context_length_exceeded"})`. Assert `ContextOverflowError`.
- `test_stream_auth_error_not_classified_as_overflow` — mock raises `openai.AuthenticationError("Invalid token")`. Assert it propagates as-is, NOT as `ContextOverflowError`.
- `test_count_tokens_uses_tiktoken_when_available` — set model to `"gpt-4o-mini"`. Assert non-zero count for non-trivial messages.
- `test_count_tokens_fallback_for_unknown_model` — set model to `"some-ollama-model"`. Assert returns approximate count (no exception).

#### 4c. `tests/test_spawn.py` additions
- `test_spawn_default_tools_excludes_spawn` — register `Spawn` + a regular tool in parent. Sub-agent with `args.tools=None` should see the regular tool but not `Spawn` itself.
- `test_spawn_explicit_tool_list` — `args.tools=["read"]` filters to only that tool.
- `test_spawn_unknown_tool_in_args_silently_skipped` — `args.tools=["nonexistent"]` doesn't crash; sub-agent has zero tools.

#### 4d. `tests/test_tools.py` additions
- `test_partition_unknown_tool_serializes` — call references a tool not in registry. Assert it goes in an unsafe (serial) batch.
- `test_partition_invalid_args_serializes` — call args fail Pydantic validation. Assert serial batch.
- `test_drain_with_synthetic_errors_yields_for_unstarted` — queue an unsafe call (so it's never started), then `drain_with_synthetic_errors`. Assert one synthetic `ToolResult` is yielded.
- `test_truncation_with_non_string_output` — tool returns a dict bigger than 50KB after JSON encoding. Assert truncation kicks in.

#### 4e. `tests/test_compact.py` additions
- `test_compact_no_op_when_short_history` — `len(messages) <= COMPACT_KEEP_TAIL` AND over threshold. Assert no compaction (no summarizer call).
- `test_compact_signature_drops_system_param` — call `maybe_compact(state, llm)` with two positional args only. Assert it works (regression test for the cleanup).

---

## Critical files to modify

| Path | Change |
|---|---|
| `agent/loop.py` | Wrap body in try/finally for executor cleanup; drop 3rd arg from `maybe_compact` calls; thread `expose_tool_errors` |
| `agent/llm.py` | Replace string-match overflow detection with `BadRequestError`-typed branch; accept `_client` for DI |
| `agent/tools.py` | Sanitize `_run_one` error path (default + opt-in); thread flag via `ToolContext`; (optional) cache parsed args from partition |
| `agent/compact.py` | Drop `system` parameter and dead `_ = system` line |
| `agent/builtin/spawn.py` | `tools: list[str] \| None = None` default; "all parent tools except Spawn" semantics when None |
| `agent/__init__.py` | Add docstring; re-export anything new |
| `agent/py.typed` | New empty file |
| `pyproject.toml` | `force-include` clause for `py.typed` |
| `README.md` | Full rewrite per 2a |
| `tests/test_loop.py`, `tests/test_spawn.py`, `tests/test_tools.py`, `tests/test_compact.py` | New tests per §4 |
| `tests/test_llm.py` | New file per 4b |

## Existing patterns to reuse

- `StreamingExecutor.drain_with_synthetic_errors` (`agent/tools.py:177`) — already handles cancel-and-yield. Reuse for the loop's `finally` cleanup; no new code needed.
- `ContextOverflowError` (`agent/llm.py:11`) — keep the exception class; only the detection logic changes.
- `ToolContext` (`agent/tools.py:10`) — add `expose_tool_errors: bool = False` field; the class is already extensible by design.
- The `FakeLLM` / `ScriptedTurn` pattern in `tests/test_loop.py:18-53` — reuse for the new loop tests; no need to redesign.
- The `ScriptedLLM` and `Dispatcher` patterns in `tests/test_spawn.py` — reuse for new spawn tests.

## Verification

End-to-end:
```bash
uv sync --group dev
.venv/bin/pytest tests/ -v --ignore=tests/integration
```
Expected: all prior 21 tests still pass + roughly 18 new tests pass (≈39 total).

Smoke check the README example:
```bash
.venv/bin/python -c "
import asyncio
from agent import run, ToolRegistry
async def main():
    print('imports ok')
asyncio.run(main())
"
```

Distribution check:
```bash
uv build
unzip -l dist/agent-0.1.0-py3-none-any.whl | grep py.typed
```
Expected: `agent/py.typed` is present in the wheel.

## Explicit non-goals (skipped intentionally)

- **Default timeouts on tools / LLM streams** — user opted out; signal-based cancellation is enough for v1.
- **CI workflow (GitHub Actions)** — defer until there are contributors. Manual `pytest` is fine for solo dev.
- **Generic `Tool[T: BaseModel]` Protocol** — Pyright invariance warnings are cosmetic; runtime is correct.
- **Retry / backoff / rate limiting** — caller's responsibility. Wrap `run()` in your own retry if you need it.
- **Live integration tests in CI** — `tests/integration/test_ollama.py` runs manually with `OLLAMA_HOST` set.
- **Coverage tooling (`pytest-cov`)** — adds infra without changing behavior. Add when there's a coverage threshold to enforce.
