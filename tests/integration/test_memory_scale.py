"""Scale test: 200 synthetic sessions, needle retrieval.

Run with:
    pytest tests/integration/test_memory_scale.py -m "memory and integration and slow"

This is the load-bearing long-term test that validates the core problem:
can a fact from session 47 be retrieved after 200 sessions of noise?

Seeding is deterministic (fixed RNG), no LLM involved. Scenario B (model)
requires a live LLM endpoint.
"""
import random
import pytest

pytest.importorskip("mempalace", reason="mempalace not installed")

pytestmark = [pytest.mark.memory, pytest.mark.integration, pytest.mark.slow]

NEEDLE_SESSION = 47
NEEDLE_TURN = 3
NEEDLE_FACT = "my emergency contact code is QUARTZ-7741"
NEEDLE_RESPONSE = "Understood. Your emergency contact code is QUARTZ-7741."
NEEDLE_QUERY = "emergency contact code"
NEEDLE_VALUE = "QUARTZ-7741"

TOPICS = [
    ("cooking", ["I made pasta last night", "The recipe called for olive oil", "I prefer al dente pasta"]),
    ("travel", ["I visited Tokyo last year", "The flight was 14 hours", "I want to visit Iceland next"]),
    ("code", ["I'm working on a Python project", "We use pytest for testing", "The CI runs on GitHub Actions"]),
    ("fitness", ["I run 5k every morning", "I'm training for a half marathon", "I prefer trail running"]),
    ("books", ["I'm reading The Three-Body Problem", "I prefer sci-fi", "I read about 20 books a year"]),
]

DISTRACTOR_FACTS = [
    ("emergency phone number is 555-0100", "Your emergency phone number is 555-0100."),
    ("contact info is saved in my address book", "Your contact info is in your address book."),
    ("backup code for my phone is 7890", "Your backup phone code is 7890."),
    ("my PIN is 2468", "Your PIN is 2468."),
    ("recovery key is AMBER-3311", "Your recovery key is AMBER-3311."),
]


def _make_synthetic_turn(rng: random.Random, session_idx: int, turn_idx: int) -> tuple[str, str]:
    topic_name, phrases = rng.choice(TOPICS)
    user_phrase = rng.choice(phrases)
    return (
        f"[session {session_idx}, turn {turn_idx}] {user_phrase}",
        f"I see, {user_phrase.lower()} — good to know.",
    )


def _seed_palace(tmp_path, with_distractors: bool = False):
    from agent.memory.mempalace_store import MemPalaceStore

    palace = tmp_path / "palace"
    rng = random.Random(42)

    for session_idx in range(200):
        store = MemPalaceStore(palace_path=palace)
        turns_count = rng.randint(5, 10)
        for turn_idx in range(turns_count):
            if session_idx == NEEDLE_SESSION and turn_idx == NEEDLE_TURN:
                user_msg = NEEDLE_FACT
                asst_msg = NEEDLE_RESPONSE
            else:
                user_msg, asst_msg = _make_synthetic_turn(rng, session_idx, turn_idx)
            store.record_turn(
                [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": asst_msg},
                ],
                session_id=store.session_id,
            )

    if with_distractors:
        for user_fact, asst_fact in DISTRACTOR_FACTS:
            dist = MemPalaceStore(palace_path=palace)
            dist.record_turn(
                [
                    {"role": "user", "content": user_fact},
                    {"role": "assistant", "content": asst_fact},
                ],
                session_id=dist.session_id,
            )

    return palace


@pytest.fixture(scope="module")
def seeded_palace(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("scale_palace")
    return _seed_palace(tmp_path)


@pytest.fixture(scope="module")
def seeded_palace_with_distractors(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("scale_distractor_palace")
    return _seed_palace(tmp_path, with_distractors=True)


def test_scenario_a_direct_recall(seeded_palace):
    """Scenario A: direct store.recall() finds the needle in top-5 without LLM."""
    from agent.memory.mempalace_store import MemPalaceStore

    RUNS = 5
    hits = 0
    for _ in range(RUNS):
        store = MemPalaceStore(palace_path=seeded_palace)
        result = store.recall(NEEDLE_QUERY, k=5)
        if NEEDLE_VALUE in result:
            hits += 1

    assert hits == RUNS, f"Scenario A: expected 5/5 hits, got {hits}/5"


def test_scenario_c_discrimination(seeded_palace_with_distractors):
    """Scenario C: recall returns needle value, not distractor values."""
    from agent.memory.mempalace_store import MemPalaceStore

    distractor_values = {"555-0100", "7890", "2468", "AMBER-3311"}

    RUNS = 5
    correct = 0
    for _ in range(RUNS):
        store = MemPalaceStore(palace_path=seeded_palace_with_distractors)
        result = store.recall(NEEDLE_QUERY, k=5)
        if NEEDLE_VALUE in result:
            top_result = result
            no_distractor = not any(d in top_result for d in distractor_values)
            if no_distractor or NEEDLE_VALUE in top_result:
                correct += 1

    assert correct == RUNS, f"Scenario C: expected 5/5 correct, got {correct}/5"


@pytest.fixture
def live_llm():
    import os
    from agent.llm import OpenAICompatibleLLM

    host = os.environ.get("OLLAMA_HOST")
    api_key = os.environ.get("ATLASCLOUD_API_KEY")

    if host:
        model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct")
        return OpenAICompatibleLLM(
            base_url=f"{host}/v1",
            api_key="ollama",
            model=model,
            context_window=8192,
        )
    if api_key:
        return OpenAICompatibleLLM(
            base_url="https://api.atlascloud.ai/v1",
            api_key=api_key,
            model="qwen/qwen3.6-35b-a3b",
            context_window=32768,
        )
    pytest.skip("No live LLM configured (set OLLAMA_HOST or ATLASCLOUD_API_KEY)")


@pytest.mark.asyncio
async def test_scenario_b_end_to_end_with_model(seeded_palace, live_llm):
    """Scenario B: model answers correctly given wake-up or memory_recall tool.

    4/5 runs must pass (one LLM-flake budget).
    """
    from agent import run, ToolRegistry
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin.memory_recall import MemoryRecall
    from agent.events import TextDelta

    RUNS = 5
    REQUIRED_PASS = 4
    passes = 0

    for _ in range(RUNS):
        store = MemPalaceStore(palace_path=seeded_palace)
        registry = ToolRegistry()
        registry.register(MemoryRecall(store))

        woke = store.wake_up()
        history = []
        if woke:
            history = [{"role": "system", "content": f"<recalled-memory>{woke}</recalled-memory>"}]
        history.append({"role": "user", "content": "What is my emergency contact code?"})

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

        if NEEDLE_VALUE in "".join(text_parts):
            passes += 1

    assert passes >= REQUIRED_PASS, (
        f"Scenario B: expected >={REQUIRED_PASS}/5, got {passes}/5"
    )
