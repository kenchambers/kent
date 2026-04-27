from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..state import Message


@runtime_checkable
class MemoryStore(Protocol):
    """Strategy interface. Swap implementations without touching the loop."""

    @property
    def session_id(self) -> str: ...

    def record_turn(self, messages: list["Message"], *, session_id: str) -> None: ...
    """Called after every turn (any terminal reason) with messages added this turn.
    Must not raise — backend errors are swallowed and logged."""

    def wake_up(self) -> str: ...
    """Return global recall text for compaction summaries."""

    def recall(self, query: str, k: int = 5) -> str: ...
    """Backing call for memory_recall tool. Returns model-ready text."""


@runtime_checkable
class WingedMemoryStore(MemoryStore, Protocol):
    """Optional extension of MemoryStore with wing + diary support.

    Tools that need wing capability isinstance-check for this Protocol before
    exposing wing arguments. External MemoryStore implementers that don't need
    wings are unaffected — the base MemoryStore Protocol is unchanged.
    """

    @property
    def active_wing(self) -> str: ...

    @property
    def kent_home(self) -> Path: ...
    """Root directory holding active_wing.txt + diaries/<wing>/. Lets callers locate
    wing/diary state without reaching into private attributes."""

    @property
    def palace_path(self) -> Path: ...
    """ChromaDB palace directory used by this store."""

    @property
    def transcript_path(self) -> Path: ...
    """JSONL transcript file for the current session."""

    def set_active_wing(self, name: str) -> None: ...

    def recall_in_wing(self, query: str, k: int = 5) -> str: ...
    """Wing-scoped diary recall. Backing call for memory_recall_here tool."""

    def write_diary(self, kind: str, text: str, *, topic: str | None = None) -> None: ...
    """Append an entry to the active wing's daily diary file and ingest it."""

    def wake_up_full(self) -> str: ...
    """Global wake-up + wing-scoped diary recall concatenated. Used at session start."""
