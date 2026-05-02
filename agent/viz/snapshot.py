import logging
import time
from pathlib import Path

from agent.memory.wings import list_wings

logger = logging.getLogger(__name__)
DRAWER_HARD_CAP = 5_000
KENT_HOME = Path.home() / ".kent"

_first_seen: dict[str, float] = {}


def _stamp_first_seen(node: dict) -> dict:
    nid = node["id"]
    node["first_seen"] = _first_seen.setdefault(nid, time.time())
    return node


def build_snapshot(palace_path: Path, kent_home: Path) -> dict:
    nodes: list[dict] = []
    links: list[dict] = []
    error: str | None = None
    truncated = False

    if not (palace_path / "chroma.sqlite3").exists():
        return {"nodes": [], "links": [], "empty": True}

    try:
        from mempalace.palace import get_collection, get_closets_collection
        import mempalace.palace_graph as palace_graph

        col = get_collection(str(palace_path), create=False)

        # Identity sphere — kept small so it doesn't visually swallow drawers
        # that orbit it as orphans.
        nodes.append(_stamp_first_seen({
            "id": "identity", "type": "identity",
            "label": "kent", "color": "gold", "val": 3,
        }))

        # CRITICAL (R2): pass col= explicitly so build_graph reads kent's
        # palace, not mempalace's default at ~/.mempalace/palace.
        rooms, edges = palace_graph.build_graph(col=col)

        for wing in list_wings(home=kent_home):
            nodes.append(_stamp_first_seen({
                "id": f"wing:{wing}", "type": "wing",
                "label": wing, "color": "cyan", "val": 4,
            }))

        for room, data in rooms.items():
            nodes.append(_stamp_first_seen({
                "id": f"room:{room}", "type": "room",
                "label": room, "color": "purple",
                "val": 1 + len(data.get("wings", [])) * 0.1,
            }))
            for w in data["wings"]:
                links.append({"source": f"wing:{w}", "target": f"room:{room}"})

        # FIXED (R3): connect the two wings through the shared room,
        # not a self-loop on the room node.
        for e in edges:
            links.append({
                "source": f"wing:{e['wing_a']}",
                "target": f"wing:{e['wing_b']}",
                "type": "passive_tunnel",
                "label": e["room"],
                "color": "#888",
            })

        # FIXED (R9): single DRAWER_HARD_CAP constant.
        batch = col.get(include=["metadatas"], limit=DRAWER_HARD_CAP + 1)
        ids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        if len(ids) > DRAWER_HARD_CAP:
            truncated = True
            ids, metas = ids[:DRAWER_HARD_CAP], metas[:DRAWER_HARD_CAP]

        # Track wings mentioned by drawer metadata that aren't already in
        # list_wings(). Without this, drawers with wing="kent_default" but
        # no ~/.kent/diaries/kent_default/ dir would be orphan-linked.
        known_room_ids = {f"room:{r}" for r in rooms}
        seen_wing_ids = {n["id"] for n in nodes if n["type"] == "wing"}

        for did, meta in zip(ids, metas):
            source_label = (meta or {}).get("source_label", "")
            if source_label == "code":
                kind_color = "#fc6"
                val_boost = 0.3
            else:
                kind_color = {
                    "OBSERVATION": "#5af", "FINDING": "#5f8",
                    "DECISION": "#fa5", "PATTERN": "#a5f",
                }.get((meta or {}).get("kind", ""), "#bbb")
                val_boost = float((meta or {}).get("importance", 1.0)) * 0.4
            nodes.append(_stamp_first_seen({
                "id": f"drawer:{did}", "type": "drawer",
                "label": (meta or {}).get("topic") or did[:8],
                "color": kind_color, "val": 1.0 + val_boost,
                "source_label": source_label,
            }))
            room = (meta or {}).get("room") or ""
            wing = (meta or {}).get("wing") or ""
            room_id = f"room:{room}" if room else ""

            if room_id and room_id in known_room_ids:
                links.append({"source": room_id, "target": f"drawer:{did}"})
            elif wing:
                # Fallback 1: room missing or filtered (e.g. "general").
                # Link the drawer to its wing.
                wing_id = f"wing:{wing}"
                if wing_id not in seen_wing_ids:
                    nodes.append(_stamp_first_seen({
                        "id": wing_id, "type": "wing",
                        "label": wing, "color": "cyan", "val": 4,
                    }))
                    seen_wing_ids.add(wing_id)
                links.append({"source": wing_id, "target": f"drawer:{did}"})
            else:
                # Fallback 2: no wing, no usable room. Anchor to identity so
                # the drawer doesn't stack at origin behind the identity glyph.
                links.append({
                    "source": "identity",
                    "target": f"drawer:{did}",
                    "type": "orphan",
                    "color": "#7a8090",
                })

        try:
            closets_col = get_closets_collection(str(palace_path), create=False)
            cb = closets_col.get(include=["documents", "metadatas"], limit=DRAWER_HARD_CAP)
            for cid, doc in zip(cb.get("ids") or [], cb.get("documents") or []):
                first_line = (doc or "").splitlines()[:1]
                topic = first_line[0].split("|", 1)[0].strip() if first_line else ""
                label = topic[:40] or "closet"
                nodes.append(_stamp_first_seen({
                    "id": f"closet:{cid}", "type": "closet",
                    "label": label, "color": "#aaa", "val": 0.7,
                }))
                for line in (doc or "").splitlines():
                    arrow = line.find("→")
                    if arrow < 0:
                        continue
                    for ref in line[arrow + 1:].split(","):
                        ref = ref.strip()
                        if ref:
                            links.append({
                                "source": f"closet:{cid}",
                                "target": f"drawer:{ref}",
                                "type": "closet_ref",
                            })
        except Exception:
            logger.debug("closets not available", exc_info=True)

        for wing in list_wings(home=kent_home):
            wing_dir = kent_home / "diaries" / wing
            if not wing_dir.exists():
                continue
            for md in wing_dir.glob("*.md"):
                nid = f"diary:{wing}/{md.name}"
                nodes.append(_stamp_first_seen({
                    "id": nid, "type": "diary_file",
                    "label": md.name, "color": "#5f5", "val": 1.0,
                }))
                links.append({"source": f"wing:{wing}", "target": nid})

        # FIXED (R4): tunnel records have nested source/target dicts.
        # Materialize wing nodes on demand: explicit tunnels can reference
        # wings that don't have a diary dir yet, but we still want the edge
        # to render (3d-force-graph silently drops links to missing nodes).
        for t in palace_graph.list_tunnels():
            for endpoint in (t["source"], t["target"]):
                wing_id = f"wing:{endpoint['wing']}"
                if wing_id not in seen_wing_ids:
                    nodes.append(_stamp_first_seen({
                        "id": wing_id, "type": "wing",
                        "label": endpoint["wing"], "color": "cyan", "val": 4,
                    }))
                    seen_wing_ids.add(wing_id)
            links.append({
                "source": f"wing:{t['source']['wing']}",
                "target": f"wing:{t['target']['wing']}",
                "type": "tunnel",
                "label": t.get("label", ""),
                "color": "red",
            })

    except Exception as e:
        # FIXED (R7): never kill the SSE thread. Return what we have plus
        # an error tag the UI can surface in the HUD.
        error = f"{type(e).__name__}: {e}"
        logger.exception("snapshot build failed")

    # Pathway overlay: annotate wings/queries that triggered repeated training
    pathway_data = _build_pathway_overlay(kent_home)

    return {
        "nodes": nodes, "links": links,
        "truncated": truncated, "error": error,
        "pathways": pathway_data,
    }


def _build_pathway_overlay(kent_home: Path) -> dict:
    """Return pathway summary data for the viz HUD and node annotations."""
    try:
        from agent.training.scout import read_pathways, PATHWAYS_PATH
        pathways = read_pathways(PATHWAYS_PATH)
        if not pathways:
            return {"count": 0, "by_wing": {}, "by_resource": {}}

        by_wing: dict[str, int] = {}
        by_resource: dict[str, int] = {}
        recent: list[dict] = []

        for p in pathways:
            wing = p.get("wing") or "unknown"
            resource = p.get("resource", "")
            by_wing[wing] = by_wing.get(wing, 0) + 1
            by_resource[resource] = by_resource.get(resource, 0) + 1

        # Most recent 5 pathways for HUD
        sorted_p = sorted(pathways, key=lambda x: x.get("created_at", 0), reverse=True)
        for p in sorted_p[:5]:
            recent.append({
                "wing": p.get("wing"),
                "resource": p.get("resource"),
                "drift_detected": p.get("drift_detected"),
                "created_at": p.get("created_at"),
            })

        return {
            "count": len(pathways),
            "by_wing": by_wing,
            "by_resource": by_resource,
            "recent": recent,
        }
    except Exception:
        logger.debug("pathway overlay build failed", exc_info=True)
        return {"count": 0, "by_wing": {}, "by_resource": {}}
