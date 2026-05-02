from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .transcript import append_messages

if TYPE_CHECKING:
    from ..state import Message

logger = logging.getLogger(__name__)

# Process-global lock that serializes the mempalace `sweep()` call. Without it,
# parallel workers writing transcripts can race on the palace store. Reads tolerate
# slightly-stale state and don't need locking.
_SWEEP_LOCK = threading.Lock()

# Track wings we've already emitted a graph-cache invalidation for this process.
# Used to limit graph invalidation to the *first* turn in a new wing (cheap check).
_INVALIDATED_WINGS: set[str] = set()


def _tag_session_drawers(
    palace_path: Path,
    session_id: str,
    message_uuids: list[str],
    *,
    wing: str,
    room: str,
) -> None:
    """Post-tag drawer metadata with wing+room after sweep."""
    try:
        from mempalace.palace import get_collection
        from mempalace.sweeper import _drawer_id_for_message

        col = get_collection(str(palace_path), create=False)
        ids = [_drawer_id_for_message(session_id, u) for u in message_uuids]
        existing = col.get(ids=ids, include=["metadatas"])
        merged = []
        for m in (existing.get("metadatas") or []):
            m = dict(m or {})
            m["wing"] = wing
            m["room"] = room
            merged.append(m)
        if merged:
            col.update(ids=existing.get("ids") or [], metadatas=merged)
    except Exception:
        logger.warning("_tag_session_drawers failed", exc_info=True)

_TRANSCRIPT_BASE = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "kent" / "transcripts"

# Kent owns its own palace at ~/.kent/palace rather than sharing
# ~/.mempalace/palace with other mempalace tools. Reasons:
#   - sweeper.sweep() does not write a `wing` field to drawer metadata,
#     so the documented wing-based isolation does not apply to our ingest path
#   - a separate palace gives us total physical isolation: no cross-contamination
#     from `mempalace mine`, the MCP server, or other mempalace consumers
_DEFAULT_KENT_HOME = Path(os.environ.get("KENT_HOME", str(Path.home() / ".kent")))
_DEFAULT_PALACE = _DEFAULT_KENT_HOME / "palace"


class MemPalaceStore:
    """MemPalace-backed persistent memory with wing + diary support."""

    def __init__(
        self,
        *,
        palace_path: Path | None = None,
        kent_home: Path | None = None,
    ) -> None:
        from .wings import read_active_wing

        self._kent_home: Path = kent_home if kent_home is not None else _DEFAULT_KENT_HOME
        self._palace_path: Path = palace_path if palace_path is not None else self._kent_home / "palace"
        self._session_id: str = uuid.uuid4().hex
        self._transcript_path: Path = _TRANSCRIPT_BASE / f"{self._session_id}.jsonl"
        self._active_wing: str = read_active_wing(home=self._kent_home)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def active_wing(self) -> str:
        return self._active_wing

    @property
    def kent_home(self) -> Path:
        return self._kent_home

    @property
    def palace_path(self) -> Path:
        return self._palace_path

    @property
    def transcript_path(self) -> Path:
        return self._transcript_path

    def set_active_wing(self, name: str) -> None:
        from .wings import sanitize_wing, write_active_wing

        clean = sanitize_wing(name)
        self._active_wing = clean
        write_active_wing(clean, home=self._kent_home)

    def record_turn(self, messages: list["Message"], *, session_id: str, room: str = "conversation") -> None:
        if not messages:
            return
        try:
            from mempalace.sweeper import sweep

            uuids = append_messages(self._transcript_path, messages, session_id=session_id)
            with _SWEEP_LOCK:
                sweep(str(self._transcript_path), str(self._palace_path), source_label="kent")
                _tag_session_drawers(
                    self._palace_path, session_id, uuids,
                    wing=self._active_wing, room=room,
                )
                # Invalidate the graph cache once per wing per process so kent viz
                # reflects new drawers within ~1 s rather than waiting out the TTL.
                if self._active_wing not in _INVALIDATED_WINGS:
                    try:
                        from mempalace.palace_graph import invalidate_graph_cache
                        invalidate_graph_cache()
                        _INVALIDATED_WINGS.add(self._active_wing)
                    except Exception:
                        logger.debug("graph cache invalidation failed", exc_info=True)
        except Exception:
            logger.warning("MemPalaceStore.record_turn failed", exc_info=True)

    def fork(self, *, session_id: str) -> "MemPalaceStore":
        """Return a sibling store with the given session_id.

        Same palace, same kent_home, same active wing — only the session_id and
        transcript path differ. Used by spawned subagents so they get their own
        per-session transcript file but write to the shared palace.
        """
        sibling = MemPalaceStore(
            palace_path=self._palace_path,
            kent_home=self._kent_home,
        )
        sibling._session_id = session_id
        sibling._transcript_path = _TRANSCRIPT_BASE / f"{session_id}.jsonl"
        sibling._active_wing = self._active_wing
        return sibling

    def wake_up(self) -> str:
        """Global-only wake-up. Used by compact.py — keeps compaction tokens bounded."""
        try:
            from mempalace.layers import MemoryStack

            return MemoryStack(str(self._palace_path)).wake_up()
        except Exception:
            logger.warning("MemPalaceStore.wake_up failed", exc_info=True)
            return ""

    def wake_up_full(self) -> str:
        """Global wake-up + wing-scoped diary recall. Used at session start only."""
        global_text = self.wake_up()

        wing_text = ""
        try:
            from mempalace.layers import MemoryStack

            result = MemoryStack(str(self._palace_path)).recall(
                wing=self._active_wing, room="daily", n_results=10
            )
            stripped = (result or "").strip()
            if stripped and not stripped.startswith(("No drawers found", "No palace")):
                wing_text = stripped
        except Exception:
            logger.warning(
                "MemPalaceStore.wake_up_full wing-scoped recall failed", exc_info=True
            )

        if global_text and wing_text:
            return global_text + "\n\n" + wing_text
        return global_text or wing_text

    def recall(self, query: str, k: int = 5) -> str:
        try:
            from mempalace.layers import Layer3

            return Layer3(str(self._palace_path)).search(query, n_results=k)
        except Exception:
            logger.warning("MemPalaceStore.recall failed", exc_info=True)
            return ""

    def recall_in_wing(self, query: str, k: int = 5) -> str:
        """Wing-scoped diary recall via Layer3 with wing + room filter."""
        try:
            from mempalace.layers import Layer3

            return Layer3(str(self._palace_path)).search(
                query, wing=self._active_wing, room="daily", n_results=k
            )
        except Exception:
            logger.warning("MemPalaceStore.recall_in_wing failed", exc_info=True)
            return ""

    def write_diary(self, kind: str, text: str, *, topic: str | None = None) -> None:
        try:
            from .diary import DiaryWriter

            DiaryWriter(self._palace_path, self._kent_home).write(
                self._active_wing, kind, text, topic=topic
            )
        except Exception:
            logger.warning("MemPalaceStore.write_diary failed", exc_info=True)
