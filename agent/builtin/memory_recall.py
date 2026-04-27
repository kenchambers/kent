from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..memory.store import MemoryStore
    from ..tools import ToolContext


class MemoryRecall:
    """Search long-term memory for relevant facts from previous sessions."""

    name = "memory_recall"
    description = (
        "Search long-term memory for information relevant to a query. "
        "Use this to recall facts, preferences, or context from previous sessions."
    )

    class Args(BaseModel):
        query: str
        k: int = 5

    input_model = Args

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    def is_concurrency_safe(self, args: Args) -> bool:
        return True

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        result = self._store.recall(args.query, k=args.k)
        return ToolResult(call_id="", output=result or "(no memories found)")
