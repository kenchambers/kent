from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..state import Message


class MemoryStore(Protocol):
    """Strategy interface. Swap implementations without touching the loop."""

    @property
    def session_id(self) -> str: ...

    def record_turn(self, messages: list["Message"], *, session_id: str) -> None: ...
    """Called after every turn (any terminal reason) with messages added this turn.
    Must not raise — backend errors are swallowed and logged."""

    def wake_up(self) -> str: ...
    """Return recall text injected at session start and embedded into compaction summaries."""

    def recall(self, query: str, k: int = 5) -> str: ...
    """Backing call for memory_recall tool. Returns model-ready text."""
