from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..memory.store import WingedMemoryStore
    from ..tools import ToolContext


class DiaryWrite:
    """Append an entry to the active wing's agent diary."""

    name = "diary_write"
    description = (
        "Write a diary entry to the active wing's agent notebook. "
        "Use kind=OBSERVATION for things you noticed, FINDING for conclusions, "
        "DECISION for choices made, PATTERN for recurring themes. "
        "Entries are persisted across sessions and searchable via memory_recall_here."
    )

    class Args(BaseModel):
        kind: Literal["OBSERVATION", "FINDING", "DECISION", "PATTERN"]
        text: str
        topic: str | None = None

    input_model = Args

    def __init__(self, store: "WingedMemoryStore") -> None:
        self._store = store

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        self._store.write_diary(args.kind, args.text, topic=args.topic)
        topic_part = f" [{args.topic}]" if args.topic else ""
        return ToolResult(
            call_id="",
            output=f"[diary] {args.kind}{topic_part} recorded in wing '{self._store.active_wing}'.",
        )
