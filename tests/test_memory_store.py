"""Unit tests for MemPalaceStore: JSONL writing + sweep integration."""
import json
import pytest

mempalace = pytest.importorskip("mempalace", reason="mempalace not installed")


@pytest.fixture()
def fake_sweep(monkeypatch):
    """Patch mempalace.sweeper.sweep to a no-op and capture calls."""
    import mempalace.sweeper

    calls: list[tuple] = []

    def _fake(jsonl_path, palace_path, source_label=None):
        calls.append((jsonl_path, palace_path, source_label))
        return {}

    monkeypatch.setattr(mempalace.sweeper, "sweep", _fake)
    return calls


@pytest.fixture()
def fake_memory_stack(monkeypatch):
    import mempalace.layers

    class _FakeStack:
        def __init__(self, palace_path):
            pass

        def wake_up(self, wing=None):
            return "L0+L1 test recall"

    monkeypatch.setattr(mempalace.layers, "MemoryStack", _FakeStack)
    return "L0+L1 test recall"


@pytest.fixture()
def fake_layer3(monkeypatch):
    import mempalace.layers

    class _FakeL3:
        def __init__(self, palace_path):
            pass

        def search(self, query, wing=None, n_results=5, **_):
            return f"results for: {query}"

    monkeypatch.setattr(mempalace.layers, "Layer3", _FakeL3)
    return _FakeL3


def test_record_turn_writes_one_line_per_message(tmp_path, fake_sweep):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    store.record_turn(messages, session_id=store.session_id)

    transcript = store._transcript_path
    assert transcript.exists()
    lines = [l for l in transcript.read_text().strip().split("\n") if l]
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert "sessionId" in rec
        assert "uuid" in rec
        assert "timestamp" in rec
        assert "message" in rec


def test_record_turn_calls_sweep_exactly_once_per_turn(tmp_path, fake_sweep):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
    store.record_turn(msgs, session_id=store.session_id)

    assert len(fake_sweep) == 1


def test_record_turn_two_calls_means_two_sweeps(tmp_path, fake_sweep):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    store.record_turn([{"role": "user", "content": "a"}], session_id=store.session_id)
    store.record_turn([{"role": "assistant", "content": "b"}], session_id=store.session_id)

    assert len(fake_sweep) == 2


def test_record_turn_skips_empty_messages(tmp_path, fake_sweep):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    store.record_turn([], session_id=store.session_id)

    assert len(fake_sweep) == 0


def test_record_turn_swallows_sweep_exceptions(tmp_path, monkeypatch):
    import mempalace.sweeper
    from agent.memory.mempalace_store import MemPalaceStore

    def _boom(*a, **kw):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(mempalace.sweeper, "sweep", _boom)

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    store.record_turn([{"role": "user", "content": "x"}], session_id=store.session_id)
    # Must not raise


def test_wake_up_returns_string(tmp_path, fake_memory_stack):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    result = store.wake_up()
    assert isinstance(result, str)
    assert "L0+L1 test recall" in result


def test_wake_up_swallows_exceptions(tmp_path, monkeypatch):
    import mempalace.layers
    from agent.memory.mempalace_store import MemPalaceStore

    class _BrokenStack:
        def __init__(self, p):
            pass

        def wake_up(self, w):
            raise RuntimeError("palace on fire")

    monkeypatch.setattr(mempalace.layers, "MemoryStack", _BrokenStack)
    store = MemPalaceStore(palace_path=tmp_path / "palace")
    assert store.wake_up() == ""


def test_recall_returns_string(tmp_path, fake_layer3):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    result = store.recall("test query", k=3)
    assert isinstance(result, str)
    assert "test query" in result


def test_recall_swallows_exceptions(tmp_path, monkeypatch):
    import mempalace.layers
    from agent.memory.mempalace_store import MemPalaceStore

    class _BrokenL3:
        def __init__(self, p):
            pass

        def search(self, *a, **kw):
            raise RuntimeError("search gone wrong")

    monkeypatch.setattr(mempalace.layers, "Layer3", _BrokenL3)
    store = MemPalaceStore(palace_path=tmp_path / "palace")
    assert store.recall("anything") == ""


def test_session_id_is_stable(tmp_path):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    assert store.session_id == store.session_id
    assert isinstance(store.session_id, str)
    assert len(store.session_id) > 0


def test_transcript_path_uses_session_id(tmp_path):
    from agent.memory.mempalace_store import MemPalaceStore

    store = MemPalaceStore(palace_path=tmp_path / "palace")
    assert store.session_id in str(store._transcript_path)
    assert store._transcript_path.suffix == ".jsonl"


def test_sdk_and_cli_share_default_palace(monkeypatch, tmp_path):
    """SDK (loop._default_store) and CLI both default to the same palace.

    Regression guard: if either path stops using the module default,
    `kent` REPL conversations and `agent.run()` library calls would
    write to different palaces and miss each other's memory.
    """
    monkeypatch.setenv("KENT_HOME", str(tmp_path / "kent_home"))
    import importlib
    import agent.memory.mempalace_store as store_mod
    import agent.loop as loop_mod
    importlib.reload(store_mod)
    importlib.reload(loop_mod)

    sdk_store = loop_mod._default_store()
    cli_store = store_mod.MemPalaceStore()  # what cli._repl/cmd_run construct

    assert sdk_store._palace_path == cli_store._palace_path
    assert sdk_store._palace_path == tmp_path / "kent_home" / "palace"


def test_default_palace_path_is_kent_specific(monkeypatch, tmp_path):
    """No palace_path → defaults to $KENT_HOME/palace, NOT ~/.mempalace/palace.

    Kent owns its own ChromaDB; we don't share with other mempalace consumers
    because sweeper-ingested drawers have no `wing` metadata, so wing-based
    isolation can't gate cross-contamination.
    """
    monkeypatch.setenv("KENT_HOME", str(tmp_path / "kent_home"))
    # Re-import to pick up the patched env var.
    import importlib
    import agent.memory.mempalace_store as mod
    importlib.reload(mod)

    store = mod.MemPalaceStore()
    assert store._palace_path == tmp_path / "kent_home" / "palace"
    assert ".mempalace" not in str(store._palace_path)
