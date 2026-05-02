from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext

logger = logging.getLogger(__name__)

_MAX_BYTES = 100_000


class CodeDrawer:
    """Index a source file into the memory palace as a code drawer."""

    name = "add_code_drawer"
    description = (
        "Index a source file into the active wing's memory palace as a code drawer. "
        "Useful for making code retrievable via memory_recall. "
        "Truncates at 100 KB. Not concurrency-safe."
    )

    class Args(BaseModel):
        path: str
        language: str | None = None
        wing: str | None = None
        room: str | None = None

    input_model = Args

    def __init__(self, palace_path: Path, active_wing: str) -> None:
        self._palace_path = palace_path
        self._active_wing = active_wing

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        src = Path(args.path).expanduser()
        if not src.exists():
            return ToolResult(call_id="", output=f"[code_drawer] file not found: {args.path}", is_error=True)

        try:
            raw = src.read_bytes()
        except OSError as e:
            return ToolResult(call_id="", output=f"[code_drawer] read error: {e}", is_error=True)

        # Skip binary files
        try:
            text = raw[:_MAX_BYTES].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return ToolResult(call_id="", output=f"[code_drawer] skipped binary file: {args.path}")

        truncated = len(raw) > _MAX_BYTES

        wing = args.wing or self._active_wing
        room = args.room or src.parent.name or "code"

        try:
            from mempalace.palace import get_collection

            col = get_collection(str(self._palace_path), create=True)
            doc_id = f"code_{uuid.uuid4().hex}"
            col.add(
                ids=[doc_id],
                documents=[text],
                metadatas=[{
                    "wing": wing,
                    "room": room,
                    "source_label": "code",
                    "path": str(src),
                    "language": args.language or src.suffix.lstrip(".") or "unknown",
                }],
            )
        except Exception as e:
            logger.warning("code_drawer add failed", exc_info=True)
            return ToolResult(call_id="", output=f"[code_drawer] error: {e}", is_error=True)

        trunc_note = " (truncated at 100 KB)" if truncated else ""
        return ToolResult(
            call_id="",
            output=f"[code_drawer] indexed {src.name} → wing={wing} room={room}{trunc_note}",
        )
