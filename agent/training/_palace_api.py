"""Concrete drawer/closet/tunnel access for Track 2 games.

mempalace exposes high-level retrieval (Layer3.search returns *text*, not IDs)
and low-level ChromaDB collections (drawers + closets) but no listing API on
MemoryStack. The games need: enumerate drawers, enumerate closets, semantic
search returning IDs, classify drawer source. This module provides that
without monkeying mempalace internals beyond the documented get_collection
seam.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DIARY_DRAWER_PREFIX = "drawer_diary_"
SWEEP_DRAWER_PREFIX = "sweep_"


def classify_drawer(drawer_id: str, metadata: dict | None) -> str:
    """Return 'diary', 'transcript', or 'unknown' based on stable signals."""
    if drawer_id.startswith(DIARY_DRAWER_PREFIX):
        return "diary"
    if drawer_id.startswith(SWEEP_DRAWER_PREFIX):
        return "transcript"
    if metadata:
        if metadata.get("ingest_mode") == "sweep":
            return "transcript"
        if metadata.get("source_session") == "daily_diary":
            return "diary"
    return "unknown"


def list_drawers(
    palace_path: str | Path,
    *,
    limit: int = 200,
    offset: int = 0,
    wing: str | None = None,
    room: str | None = None,
) -> list[dict[str, Any]]:
    """Return a page of drawers as dicts: {id, content, wing, room, source, metadata}."""
    try:
        from mempalace.palace import get_collection
    except Exception:
        logger.warning("mempalace.palace.get_collection unavailable")
        return []
    try:
        col = get_collection(str(palace_path), create=False)
    except Exception:
        return []

    where: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] = []
    if wing:
        conditions.append({"wing": wing})
    if room:
        conditions.append({"room": room})
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    kwargs: dict[str, Any] = {
        "include": ["documents", "metadatas"],
        "limit": limit,
        "offset": offset,
    }
    if where:
        kwargs["where"] = where

    try:
        result = col.get(**kwargs)
    except Exception:
        logger.warning("list_drawers: col.get failed", exc_info=True)
        return []

    ids = result.get("ids") or []
    docs = result.get("documents") or []
    metas = result.get("metadatas") or []
    out: list[dict[str, Any]] = []
    for i, did in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        doc = docs[i] if i < len(docs) else ""
        meta = meta or {}
        out.append({
            "id": did,
            "content": doc or "",
            "wing": meta.get("wing", ""),
            "room": meta.get("room", ""),
            "source": classify_drawer(did, meta),
            "metadata": meta,
        })
    return out


def list_closets(
    palace_path: str | Path,
    *,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return closet rows as dicts: {id, content, wing, room, metadata}."""
    try:
        from mempalace.palace import get_closets_collection
    except Exception:
        return []
    try:
        col = get_closets_collection(str(palace_path), create=False)
    except Exception:
        return []
    try:
        result = col.get(
            include=["documents", "metadatas"], limit=limit, offset=offset
        )
    except Exception:
        logger.warning("list_closets: col.get failed", exc_info=True)
        return []

    ids = result.get("ids") or []
    docs = result.get("documents") or []
    metas = result.get("metadatas") or []
    out: list[dict[str, Any]] = []
    for i, cid in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        doc = docs[i] if i < len(docs) else ""
        meta = meta or {}
        out.append({
            "id": cid,
            "content": doc or "",
            "wing": meta.get("wing", ""),
            "room": meta.get("room", ""),
            "metadata": meta,
        })
    return out


def search_drawers(
    palace_path: str | Path,
    query: str,
    *,
    k: int = 5,
    wing: str | None = None,
    room: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search returning {id, content, wing, room, similarity}.

    Layer3.search_raw drops the drawer ID, so we hit the collection directly.
    """
    try:
        from mempalace.palace import get_collection
        from mempalace.searcher import build_where_filter
    except Exception:
        return []
    try:
        col = get_collection(str(palace_path), create=False)
    except Exception:
        return []

    where = build_where_filter(wing, room)  # type: ignore[arg-type]
    kwargs: dict[str, Any] = {
        "query_texts": [query],
        "n_results": k,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where

    try:
        result = col.query(**kwargs)
    except Exception:
        logger.warning("search_drawers: col.query failed", exc_info=True)
        return []

    def _first(key: str) -> list:
        v = result.get(key) or []
        return v[0] if v and isinstance(v[0], list) else v

    ids = _first("ids")
    docs = _first("documents")
    metas = _first("metadatas")
    dists = _first("distances")
    out: list[dict[str, Any]] = []
    for i, did in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        meta = meta or {}
        doc = docs[i] if i < len(docs) else ""
        dist = dists[i] if i < len(dists) else 1.0
        out.append({
            "id": did,
            "content": doc or "",
            "wing": meta.get("wing", ""),
            "room": meta.get("room", ""),
            "similarity": round(max(0.0, 1.0 - float(dist)), 3),
            "metadata": meta,
        })
    return out


def list_palace_tunnels(wing: str | None = None) -> list[dict[str, Any]]:
    """Return tunnels filtered by wing endpoint. Empty list if unsupported."""
    try:
        from mempalace.palace_graph import list_tunnels
    except Exception:
        return []
    try:
        return list_tunnels(wing=wing) or []  # type: ignore[arg-type]
    except Exception:
        return []
