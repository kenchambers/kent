from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .transcript import append_messages

if TYPE_CHECKING:
    from ..state import Message

logger = logging.getLogger(__name__)

_TRANSCRIPT_BASE = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "kent" / "transcripts"

# Kent owns its own palace at ~/.kent/palace rather than sharing
# ~/.mempalace/palace with other mempalace tools. Reasons:
#   - sweeper.sweep() does not write a `wing` field to drawer metadata,
#     so the documented wing-based isolation does not apply to our ingest path
#   - a separate palace gives us total physical isolation: no cross-contamination
#     from `mempalace mine`, the MCP server, or other mempalace consumers
_DEFAULT_PALACE = Path(
    os.environ.get("KENT_HOME", str(Path.home() / ".kent"))
) / "palace"


class MemPalaceStore:
    """MemPalace-backed persistent memory. Default store for agent.run()."""

    def __init__(self, *, palace_path: Path | None = None) -> None:
        self._palace_path: Path = palace_path if palace_path is not None else _DEFAULT_PALACE
        self._session_id: str = uuid.uuid4().hex
        self._transcript_path: Path = _TRANSCRIPT_BASE / f"{self._session_id}.jsonl"

    @property
    def session_id(self) -> str:
        return self._session_id

    def record_turn(self, messages: list["Message"], *, session_id: str) -> None:
        if not messages:
            return
        try:
            from mempalace.sweeper import sweep
            append_messages(self._transcript_path, messages, session_id=session_id)
            sweep(str(self._transcript_path), str(self._palace_path), source_label="kent")
        except Exception:
            logger.warning("MemPalaceStore.record_turn failed", exc_info=True)

    def wake_up(self) -> str:
        try:
            from mempalace.layers import MemoryStack
            # Don't filter by wing — sweeper-stored records have no wing metadata field
            return MemoryStack(str(self._palace_path)).wake_up()
        except Exception:
            logger.warning("MemPalaceStore.wake_up failed", exc_info=True)
            return ""

    def recall(self, query: str, k: int = 5) -> str:
        try:
            from mempalace.layers import Layer3
            # Don't filter by wing — sweeper-stored records have no wing metadata field
            return Layer3(str(self._palace_path)).search(query, n_results=k)
        except Exception:
            logger.warning("MemPalaceStore.recall failed", exc_info=True)
            return ""
