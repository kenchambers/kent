"""End-to-end memory tests with a live LLM.

Run with:
    pytest tests/integration/test_memory_e2e.py -m "memory and integration"

Requires:
- mempalace installed
- OLLAMA_HOST / ATLASCLOUD_API_KEY configured (same as test_ollama.py)

These tests assert that the integration plumbing is correct — not that
the LLM always uses what we surface (that weaker guarantee is separate).
"""
import os
import pytest
from pydantic import BaseModel

pytest.importorskip("mempalace", reason="mempalace not installed")

pytestmark = [pytest.mark.memory, pytest.mark.integration]


@pytest.fixture
def live_llm():
    from agent.llm import OpenAICompatibleLLM
    from agent.cli import resolve_api_key

    host = os.environ.get("OLLAMA_HOST")

    if host:
        model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct")
        return OpenAICompatibleLLM(
            base_url=f"{host}/v1",
            api_key="ollama",
            model=model,
            context_window=8192,
        )

    api_key = resolve_api_key("atlascloud", prompt_if_missing=False)
    if api_key:
        return OpenAICompatibleLLM(
            base_url="https://api.atlascloud.ai/v1",
            api_key=api_key,
            model="qwen/qwen3.6-35b-a3b",
            context_window=32768,
        )
    pytest.skip("No live LLM configured (set OLLAMA_HOST or ATLASCLOUD_API_KEY)")


@pytest.mark.asyncio
async def test_cross_session_recall_model_driven(tmp_path, live_llm):
    """Session A records a fact; session B model surfaces it via wake-up or recall."""
    from agent import run, ToolRegistry, Terminal
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin.memory_recall import MemoryRecall
    from agent.events import TextDelta

    palace = tmp_path / "palace"

    store_a = MemPalaceStore(palace_path=palace)
    store_a.record_turn(
        [
            {"role": "user", "content": "Remember that my favorite color is octarine."},
            {"role": "assistant", "content": "Got it! I'll remember your favorite color is octarine."},
        ],
        session_id=store_a.session_id,
    )

    store_b = MemPalaceStore(palace_path=palace)
    registry = ToolRegistry()
    registry.register(MemoryRecall(store_b))

    woke = store_b.wake_up()
    history = []
    if woke:
        history = [{"role": "system", "content": f"<recalled-memory>{woke}</recalled-memory>"}]
    history.append({"role": "user", "content": "What is my favorite color?"})

    text_parts: list[str] = []
    async for ev in run(
        messages=history,
        tools=registry,
        llm=live_llm,
        max_turns=5,
        memory_store=store_b,
    ):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)

    answer = "".join(text_parts).lower()
    assert "octarine" in answer


@pytest.mark.asyncio
async def test_pre_seeded_memory_recall(tmp_path, live_llm):
    """Pre-seed facts via record_turn directly; verify model can retrieve them."""
    from agent import run, ToolRegistry
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin.memory_recall import MemoryRecall
    from agent.events import TextDelta

    palace = tmp_path / "palace"
    seeder = MemPalaceStore(palace_path=palace)

    facts = [
        ("My project code is ZEPHYR-9900", "Understood, project code is ZEPHYR-9900."),
        ("I prefer dark mode in all editors", "Noted, you prefer dark mode."),
        ("My team standup is at 9am EST", "Got it, standup at 9am EST."),
    ]
    for user_msg, asst_msg in facts:
        seeder.record_turn(
            [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": asst_msg},
            ],
            session_id=seeder.session_id,
        )

    store = MemPalaceStore(palace_path=palace)
    registry = ToolRegistry()
    registry.register(MemoryRecall(store))

    history = [{"role": "user", "content": "What is my project code?"}]
    text_parts: list[str] = []
    async for ev in run(
        messages=history,
        tools=registry,
        llm=live_llm,
        max_turns=5,
        memory_store=store,
    ):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)

    answer = "".join(text_parts)
    assert "ZEPHYR-9900" in answer


@pytest.mark.asyncio
async def test_survives_forced_compaction(tmp_path, live_llm, monkeypatch):
    """A fact stated before compaction is recalled from memory post-compaction."""
    import agent.compact as compact_module
    from agent import run, ToolRegistry
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin.memory_recall import MemoryRecall
    from agent.events import TextDelta, Terminal

    monkeypatch.setattr(compact_module, "COMPACT_THRESHOLD", 0.1)
    monkeypatch.setattr(compact_module, "COMPACT_KEEP_TAIL", 2)
    # Shrink context_window so the threshold math triggers on short filler messages.
    # context_window is only used in maybe_compact's token-ratio check, not in stream().
    monkeypatch.setattr(live_llm, "_context_window", 512)

    palace = tmp_path / "palace"
    inner = MemPalaceStore(palace_path=palace)

    inner.record_turn(
        [
            {"role": "user", "content": "My lucky number is 42."},
            {"role": "assistant", "content": "Got it, your lucky number is 42."},
        ],
        session_id=inner.session_id,
    )

    class RecordingDelegate:
        """Delegates to inner store; records every wake_up() return value."""
        def __init__(self, target):
            self._target = target
            self.wake_up_returns: list[str] = []

        @property
        def session_id(self): return self._target.session_id
        def record_turn(self, messages, *, session_id):
            self._target.record_turn(messages, session_id=session_id)
        def wake_up(self):
            text = self._target.wake_up()
            self.wake_up_returns.append(text)
            return text
        def recall(self, query, k=5):
            return self._target.recall(query, k=k)

    store = RecordingDelegate(inner)
    registry = ToolRegistry()
    registry.register(MemoryRecall(store))

    filler = [{"role": "user", "content": f"filler message {i}"} for i in range(6)]
    history = filler + [{"role": "user", "content": "What is my lucky number?"}]

    # Spy on compact summary messages — capture the summary message produced
    # by maybe_compact each time it fires.
    original_compact = compact_module.maybe_compact
    summary_messages: list[str] = []

    async def spy_compact(state, llm, *, memory_store=None):
        new_state = await original_compact(state, llm, memory_store=memory_store)
        if new_state is not state and new_state.messages:
            first = new_state.messages[0]
            if isinstance(first, dict) and first.get("role") == "system":
                summary_messages.append(first.get("content", ""))
        return new_state

    monkeypatch.setattr("agent.loop.maybe_compact", spy_compact)

    text_parts: list[str] = []
    async for ev in run(
        messages=history,
        tools=registry,
        llm=live_llm,
        max_turns=5,
        memory_store=store,
    ):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)

    answer = "".join(text_parts)

    # (a) Compaction fired and the summary message embedded recalled memory.
    assert summary_messages, "Expected at least one compaction to fire"
    assert any("<recalled-memory>" in m for m in summary_messages), (
        f"Expected <recalled-memory> in compaction summary; got: {summary_messages!r}"
    )
    assert any("42" in m for m in summary_messages), (
        "Expected the lucky-number fact to surface in the compaction summary"
    )
    # (b) The model's final answer surfaces it.
    assert "42" in answer


@pytest.mark.asyncio
async def test_fault_injection_does_not_abort_loop(tmp_path, live_llm):
    """A store whose record_turn always raises must not cause Terminal('model_error')."""
    from agent import run, ToolRegistry, Terminal
    from agent.events import TextDelta

    class BrokenStore:
        @property
        def session_id(self): return "broken"
        def record_turn(self, messages, *, session_id): raise RuntimeError("storage down")
        def wake_up(self): return ""
        def recall(self, query, k=5): return ""

    registry = ToolRegistry()
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "What is 2+2?"}],
        tools=registry,
        llm=live_llm,
        max_turns=3,
        memory_store=BrokenStore(),
    ):
        events.append(ev)

    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "completed"
