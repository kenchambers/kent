from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..memory.store import WingedMemoryStore
    from ..tools import ToolContext


class MemoryRecallHere:
    """Search long-term memory scoped to the active project wing."""

    name = "memory_recall_here"
    description = (
        "Search the active wing's diary and project memory for relevant information. "
        "Use this instead of memory_recall when the query is specific to the current project. "
        "Returns diary entries, observations, decisions, and findings recorded for this wing."
    )

    class Args(BaseModel):
        query: str
        k: int = 5

    input_model = Args

    def __init__(self, store: "WingedMemoryStore") -> None:
        self._store = store

    def is_concurrency_safe(self, args: Args) -> bool:
        return True

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        result = self._store.recall_in_wing(args.query, k=args.k)
        return ToolResult(
            call_id="",
            output=result or f"(no memories found in wing '{self._store.active_wing}')",
        )
