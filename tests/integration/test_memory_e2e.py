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
async def test_model_driven_diary_write_and_recall(tmp_path, live_llm):
    """Plan PR5 e2e: user states a fact + asks kent to remember; assert diary_write
    was invoked and the file content matches. Then in a fresh store on the same wing,
    ask "what did you note about X?" — assert the model surfaces the fact via
    memory_recall_here or via wake_up_full's wing-scoped block.
    """
    from agent import run, ToolRegistry
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin.memory_recall import MemoryRecall
    from agent.builtin.memory_recall_here import MemoryRecallHere
    from agent.builtin.diary_write import DiaryWrite
    from agent.builtin.set_wing import SetWing
    from agent.events import TextDelta, ToolCallComplete

    palace = tmp_path / "palace"
    store_a = MemPalaceStore(palace_path=palace, kent_home=tmp_path)
    store_a.set_active_wing("build-perf")

    registry_a = ToolRegistry()
    registry_a.register(MemoryRecall(store_a))
    registry_a.register(MemoryRecallHere(store_a))
    registry_a.register(DiaryWrite(store_a))
    registry_a.register(SetWing(store_a))

    system = (
        "You are kent. You have a diary_write tool. When the user shares a notable "
        "fact about the current project, you MUST record it by calling "
        "diary_write(kind='OBSERVATION', text=<one-line summary>). After recording, "
        "give a brief acknowledgement."
    )

    diary_calls_a: list[dict] = []
    async for ev in run(
        messages=[{
            "role": "user",
            "content": (
                "Please record this observation: the build pipeline got 30% slower "
                "after the switch to runner-v2. Use your diary_write tool."
            ),
        }],
        tools=registry_a,
        llm=live_llm,
        system=system,
        max_turns=4,
        memory_store=store_a,
    ):
        if isinstance(ev, ToolCallComplete) and ev.call.name == "diary_write":
            diary_calls_a.append(ev.call.arguments)

    assert diary_calls_a, "Expected the model to call diary_write at least once"

    diary_dir = tmp_path / "diaries" / "build-perf"
    md_files = list(diary_dir.glob("*.md"))
    assert md_files, f"Expected a diary .md file under {diary_dir}"
    content = md_files[0].read_text()
    assert "[OBSERVATION]" in content
    assert "[agent=kent]" in content
    assert "runner-v2" in content.lower() or "30%" in content or "slower" in content.lower()

    # Round-trip via a fresh store on the same wing — the model should be able
    # to surface the fact, either through wake_up_full or via memory_recall_here.
    store_b = MemPalaceStore(palace_path=palace, kent_home=tmp_path)
    store_b.set_active_wing("build-perf")

    registry_b = ToolRegistry()
    registry_b.register(MemoryRecall(store_b))
    registry_b.register(MemoryRecallHere(store_b))
    registry_b.register(DiaryWrite(store_b))
    registry_b.register(SetWing(store_b))

    history_b: list[dict] = []
    woke = store_b.wake_up_full()
    if woke:
        history_b = [{"role": "system", "content": f"<recalled-memory>{woke}</recalled-memory>"}]
    history_b.append({
        "role": "user",
        "content": (
            "What have you noted about the build pipeline performance? "
            "Use memory_recall_here if you need to look it up."
        ),
    })

    text_parts: list[str] = []
    async for ev in run(
        messages=history_b,
        tools=registry_b,
        llm=live_llm,
        system=system,
        max_turns=4,
        memory_store=store_b,
    ):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)

    answer = "".join(text_parts).lower()
    assert "runner-v2" in answer or "30%" in answer or "slower" in answer or "midnight" in answer, (
        f"Expected build-perf diary content to surface in the answer; got: {answer!r}"
    )


@pytest.mark.asyncio
async def test_set_wing_handshake_via_model(tmp_path, live_llm):
    """Plan PR5 e2e: user states a new project; assert at least one set_wing call
    has both name and intent after the model exchanges with the user.

    The handshake is single-call when the user supplies enough context up front:
    the model should pick a sanitized name and pass intent= on the same call.
    """
    from agent import run, ToolRegistry
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.memory.wings import list_wings, read_intent
    from agent.builtin.memory_recall import MemoryRecall
    from agent.builtin.memory_recall_here import MemoryRecallHere
    from agent.builtin.diary_write import DiaryWrite
    from agent.builtin.set_wing import SetWing
    from agent.events import ToolCallComplete

    palace = tmp_path / "palace"
    store = MemPalaceStore(palace_path=palace, kent_home=tmp_path)

    registry = ToolRegistry()
    registry.register(MemoryRecall(store))
    registry.register(MemoryRecallHere(store))
    registry.register(DiaryWrite(store))
    registry.register(SetWing(store))

    system = (
        "You are kent. When the user asks you to start tracking or monitoring a new "
        "project, you MUST call set_wing(name=<short-lowercase-slug>, "
        "intent=<one-line description>) to register it. Pick a short slug yourself "
        "based on the user's description. Wing names must be lowercase, "
        "alphanumeric, with - or _ separators (no spaces)."
    )

    set_wing_calls: list[dict] = []
    async for ev in run(
        messages=[{
            "role": "user",
            "content": (
                "Hey kent — I want you to start tracking my terraform deploy "
                "pipeline as its own project. Please register a wing for it now."
            ),
        }],
        tools=registry,
        llm=live_llm,
        system=system,
        max_turns=4,
        memory_store=store,
    ):
        if isinstance(ev, ToolCallComplete) and ev.call.name == "set_wing":
            set_wing_calls.append(ev.call.arguments)

    assert set_wing_calls, "Expected the model to call set_wing at least once"

    successful = [
        c for c in set_wing_calls
        if c.get("name") and c.get("intent")
    ]
    assert successful, (
        f"Expected at least one set_wing call with both name and intent; got: {set_wing_calls!r}"
    )

    last = successful[-1]
    wings = list_wings(home=tmp_path)
    assert last["name"].lower() in wings, (
        f"Expected wing {last['name']!r} to appear in wings dir; got {wings!r}"
    )
    intent = read_intent(last["name"].lower(), home=tmp_path)
    assert intent, "Expected .intent.txt to be written"


@pytest.mark.asyncio
async def test_diary_survives_compaction(tmp_path, live_llm, monkeypatch):
    """Plan PR5 e2e: write a fact to the diary, force compaction, model still answers
    via memory_recall_here.

    Critical invariant: compaction calls wake_up() (global only) — wing diary content
    is dropped from the summary. The model must reach back into the diary via the
    memory_recall_here tool to recover the fact.
    """
    import agent.compact as compact_module
    from agent import run, ToolRegistry
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin.memory_recall import MemoryRecall
    from agent.builtin.memory_recall_here import MemoryRecallHere
    from agent.builtin.diary_write import DiaryWrite
    from agent.builtin.set_wing import SetWing
    from agent.events import TextDelta

    monkeypatch.setattr(compact_module, "COMPACT_THRESHOLD", 0.1)
    monkeypatch.setattr(compact_module, "COMPACT_KEEP_TAIL", 2)
    monkeypatch.setattr(live_llm, "_context_window", 512)

    palace = tmp_path / "palace"
    store = MemPalaceStore(palace_path=palace, kent_home=tmp_path)
    store.set_active_wing("payments-svc")
    store.write_diary(
        "DECISION",
        "We chose stripe over braintree for payments-svc because of webhook reliability.",
        topic="vendor-choice",
    )

    registry = ToolRegistry()
    registry.register(MemoryRecall(store))
    registry.register(MemoryRecallHere(store))
    registry.register(DiaryWrite(store))
    registry.register(SetWing(store))

    # Spy on compaction summaries to verify they do NOT include the wing-scoped
    # diary content (compact uses wake_up() not wake_up_full()).
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

    system = (
        "You are kent. The active wing is 'payments-svc'. Answer questions about "
        "this project by calling memory_recall_here(query=...) when needed. "
        "Quote facts you retrieve."
    )

    filler = [{"role": "user", "content": f"filler turn {i}"} for i in range(6)]
    history = filler + [{
        "role": "user",
        "content": "Which payment vendor did we pick for payments-svc, and why?",
    }]

    text_parts: list[str] = []
    async for ev in run(
        messages=history,
        tools=registry,
        llm=live_llm,
        system=system,
        max_turns=5,
        memory_store=store,
    ):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)

    answer = "".join(text_parts).lower()

    # Compaction fired at least once.
    assert summary_messages, "Expected at least one compaction during this turn"

    # Compaction summaries must NOT carry the wing-scoped diary text — that's the
    # whole point of using wake_up() (global) rather than wake_up_full() in compact.
    for m in summary_messages:
        assert "stripe" not in m.lower() and "braintree" not in m.lower(), (
            "Compaction summary leaked wing-scoped diary content — wake_up() should "
            "be global only. Found: " + m[:300]
        )

    # The model should still surface the diary fact via memory_recall_here.
    assert "stripe" in answer, (
        f"Expected the diary fact (stripe) to surface post-compaction; got: {answer!r}"
    )


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
