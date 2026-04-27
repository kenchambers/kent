import pytest
from agent import loop as _loop_module


class NullMemoryStore:
    """Test-only no-op memory store. NOT exported from agent.memory."""

    @property
    def session_id(self) -> str:
        return "null-session"

    def record_turn(self, messages, *, session_id):
        pass

    def wake_up(self) -> str:
        return ""

    def recall(self, query: str, k: int = 5) -> str:
        return ""


@pytest.fixture(autouse=True)
def _null_memory(monkeypatch):
    """Redirect _default_store to NullMemoryStore for the whole test suite.

    Integration tests that need the real store construct MemPalaceStore()
    directly and pass it to run() — bypassing _default_store entirely.
    """
    monkeypatch.setattr(_loop_module, "_default_store", lambda: NullMemoryStore())
