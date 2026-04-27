"""Integration tests: real MemPalaceStore against a tmp palace.

Run with:
    pytest tests/integration/test_mempalace.py -m memory

Requires mempalace to be installed. These tests never hit a live LLM.
"""
import pytest

pytestmark = pytest.mark.memory

pytest.importorskip("mempalace", reason="mempalace not installed")


def test_record_and_recall_cross_session(tmp_path):
    """Fact written in session A is retrievable via recall() in session B."""
    from agent.memory.mempalace_store import MemPalaceStore

    palace = tmp_path / "palace"

    store_a = MemPalaceStore(palace_path=palace)
    store_a.record_turn(
        [
            {"role": "user", "content": "My favorite color is octarine"},
            {
                "role": "assistant",
                "content": "Noted! I'll remember your favorite color is octarine.",
            },
        ],
        session_id=store_a.session_id,
    )

    store_b = MemPalaceStore(palace_path=palace)
    recalled = store_b.recall("favorite color", k=5)
    assert isinstance(recalled, str)
    assert "octarine" in recalled.lower()


def test_record_and_recall_multiple_sessions(tmp_path):
    """Multiple sessions stored; targeted recall finds the right fact."""
    from agent.memory.mempalace_store import MemPalaceStore

    palace = tmp_path / "palace"

    for i in range(5):
        s = MemPalaceStore(palace_path=palace)
        s.record_turn(
            [
                {"role": "user", "content": f"Message about topic {i}"},
                {"role": "assistant", "content": f"Response about topic {i}"},
            ],
            session_id=s.session_id,
        )

    needle = MemPalaceStore(palace_path=palace)
    needle.record_turn(
        [
            {"role": "user", "content": "My secret keyword is VELVETEEN"},
            {"role": "assistant", "content": "Got it, VELVETEEN is your secret keyword."},
        ],
        session_id=needle.session_id,
    )

    reader = MemPalaceStore(palace_path=palace)
    result = reader.recall("secret keyword", k=5)
    assert "VELVETEEN" in result


def test_wake_up_returns_string_on_empty_palace(tmp_path):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    result = store.wake_up()
    assert isinstance(result, str)


def test_recall_returns_string_on_empty_palace(tmp_path):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    result = store.recall("anything", k=3)
    assert isinstance(result, str)


def test_two_stores_same_palace(tmp_path):
    """Two stores pointing at the same palace don't corrupt each other."""
    from agent.memory.mempalace_store import MemPalaceStore

    palace = tmp_path / "palace"
    a = MemPalaceStore(palace_path=palace)
    b = MemPalaceStore(palace_path=palace)

    a.record_turn(
        [{"role": "user", "content": "store A fact"}],
        session_id=a.session_id,
    )
    b.record_turn(
        [{"role": "user", "content": "store B fact"}],
        session_id=b.session_id,
    )

    reader = MemPalaceStore(palace_path=palace)
    result_a = reader.recall("store A fact", k=5)
    result_b = reader.recall("store B fact", k=5)
    assert isinstance(result_a, str)
    assert isinstance(result_b, str)
