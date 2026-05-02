"""kent migrate-palace — tag existing conversation drawers with wing + room metadata."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TRANSCRIPT_BASE = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "kent" / "transcripts"
_DEFAULT_KENT_HOME = Path.home() / ".kent"
_DEFAULT_PALACE = _DEFAULT_KENT_HOME / "palace"


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _session_date(records: list[dict]) -> str | None:
    """Extract YYYY-MM-DD from the earliest timestamp in the session's records."""
    for rec in records:
        ts = rec.get("timestamp", "")
        if ts:
            return ts[:10]
    return None


def _infer_wing(session_date: str | None, kent_home: Path, default_wing: str) -> str:
    """Return the wing name inferred from diary files on session_date."""
    if not session_date:
        return default_wing
    diaries_root = kent_home / "diaries"
    if not diaries_root.exists():
        return default_wing
    matches: list[str] = []
    try:
        for wing_dir in diaries_root.iterdir():
            if not wing_dir.is_dir():
                continue
            diary_file = wing_dir / f"{session_date}.md"
            if diary_file.exists():
                matches.append(wing_dir.name)
    except OSError:
        return default_wing
    if len(matches) == 1:
        return matches[0]
    return default_wing


def run_migration(
    *,
    kent_home: Path | None = None,
    palace_path: Path | None = None,
    transcript_base: Path | None = None,
    default_wing: str = "kent_default",
    dry_run: bool = False,
    regenerate_closets: bool = False,
) -> dict[str, Any]:
    """Migrate all existing conversation drawers: tag wing+room, backfill graph.

    Returns a stats dict. Idempotent — drawers that already have a wing are skipped.
    """
    from mempalace.sweeper import parse_claude_jsonl, _drawer_id_for_message
    from mempalace.palace import get_collection

    _kent_home = kent_home or _DEFAULT_KENT_HOME
    _palace_path = palace_path or _DEFAULT_PALACE
    _transcript_base = transcript_base or _TRANSCRIPT_BASE

    if not _palace_path.exists():
        return {"error": "palace does not exist", "sessions_scanned": 0, "drawers_to_update": 0}

    col = get_collection(str(_palace_path), create=False)

    jsonl_files = sorted(_transcript_base.glob("*.jsonl")) if _transcript_base.exists() else []

    sessions_scanned = 0
    drawers_to_update = 0
    wings_inferred: dict[str, int] = {}
    touched_wings: set[str] = set()

    for jsonl_path in jsonl_files:
        try:
            records = list(parse_claude_jsonl(str(jsonl_path)))
        except Exception:
            logger.debug("failed to parse %s", jsonl_path, exc_info=True)
            continue
        if not records:
            continue

        session_id = records[0].get("session_id", "")
        if not session_id:
            continue
        sessions_scanned += 1

        message_uuids = [r["uuid"] for r in records if r.get("uuid")]
        if not message_uuids:
            continue

        ids = [_drawer_id_for_message(session_id, u) for u in message_uuids]
        try:
            existing = col.get(ids=ids, include=["metadatas"])
        except Exception:
            logger.debug("col.get failed for session %s", session_id, exc_info=True)
            continue

        existing_ids = existing.get("ids") or []
        existing_metas = existing.get("metadatas") or []

        # Collect drawers that need wing assignment
        update_ids: list[str] = []
        update_metas: list[dict] = []

        session_date = _session_date(records)
        wing = _infer_wing(session_date, _kent_home, default_wing)

        for did, meta in zip(existing_ids, existing_metas):
            m = dict(meta or {})
            if m.get("wing"):
                continue  # already tagged — idempotent skip
            m["wing"] = wing
            m["room"] = "conversation"
            m["migrated_at"] = _iso_now()
            update_ids.append(did)
            update_metas.append(m)

        if update_ids:
            drawers_to_update += len(update_ids)
            wings_inferred[wing] = wings_inferred.get(wing, 0) + len(update_ids)
            touched_wings.add(wing)
            if not dry_run:
                try:
                    col.update(ids=update_ids, metadatas=update_metas)
                except Exception:
                    logger.warning("col.update failed for session %s", session_id, exc_info=True)

    # Distinct (wing, room) pairs that will become visible after migration
    graph_nodes_unlocked = len(touched_wings)  # each wing gains a room:conversation node

    if not dry_run and drawers_to_update > 0:
        try:
            from mempalace.palace_graph import invalidate_graph_cache
            invalidate_graph_cache()
        except Exception:
            logger.debug("graph cache invalidation failed", exc_info=True)

    if not dry_run and regenerate_closets and touched_wings:
        try:
            from mempalace.closet_llm import regenerate_closets as _regen
            for wing in sorted(touched_wings):
                _regen(str(_palace_path), wing=wing)
            try:
                from mempalace.palace_graph import invalidate_graph_cache
                invalidate_graph_cache()
            except Exception:
                pass
        except Exception:
            logger.warning("closet regeneration failed during migration", exc_info=True)

    return {
        "sessions_scanned": sessions_scanned,
        "drawers_to_update": drawers_to_update,
        "wings_inferred": wings_inferred,
        "graph_nodes_unlocked": graph_nodes_unlocked,
        "dry_run": dry_run,
    }
