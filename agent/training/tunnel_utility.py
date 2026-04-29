from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ._palace_api import list_palace_tunnels

logger = logging.getLogger(__name__)

_DEFAULT_METRICS_DIR = Path.home() / ".kent" / "lightning_store" / "metrics"


def _metrics_path(metrics_dir: Path | None = None) -> Path:
    base = metrics_dir or _DEFAULT_METRICS_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / "tunnel_utility.jsonl"


def log_tunnel_utility(
    rollout_id: str,
    tunnel_id: str,
    cited: bool,
    *,
    query: str = "",
    wing: str = "",
    metrics_dir: Path | None = None,
) -> None:
    """Append one tunnel-followed observation to the metrics log."""
    path = _metrics_path(metrics_dir)
    entry = {
        "ts": time.time(),
        "rollout_id": rollout_id,
        "tunnel_id": tunnel_id,
        "wing": wing,
        "cited": cited,
        "query": query[:200],
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        logger.debug("tunnel_utility log write failed", exc_info=True)


def log_rollout_tunnel_observations(
    rollout_id: str,
    transcript: list[dict[str, Any]],
    *,
    active_wing: str,
    metrics_dir: Path | None = None,
) -> int:
    """Scan a rollout transcript for memory_recall results, then log per-tunnel
    utility based on whether (a) tunneled drawers appeared in the recall and
    (b) any tunneled-drawer text was cited in the actor's response.

    Returns the number of tunnel observations logged. Plan §"Track 2 — Game D"
    says logging only, no APO — so this function never raises.
    """
    if not active_wing:
        return 0
    tunnels = list_palace_tunnels(wing=active_wing)
    if not tunnels:
        return 0

    tunneled_drawer_ids: set[str] = set()
    for t in tunnels:
        for endpoint_key in ("source", "target"):
            ep = t.get(endpoint_key) or {}
            did = ep.get("drawer_id")
            if did:
                tunneled_drawer_ids.add(did)
    if not tunneled_drawer_ids:
        return 0

    recalls: list[tuple[str, str]] = []  # (query, result_text)
    last_query = ""
    for msg in transcript:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "assistant" and isinstance(content, str):
            if "memory_recall" in content:
                last_query = content[:300]
        if role == "tool":
            text = content if isinstance(content, str) else ""
            if last_query and text:
                recalls.append((last_query, text))
                last_query = ""

    final_assistant = ""
    for msg in reversed(transcript):
        if msg.get("role") == "assistant":
            c = msg.get("content", "")
            if isinstance(c, str):
                final_assistant = c
                break

    logged = 0
    for tunnel in tunnels:
        tid = tunnel.get("id") or ""
        if not tid:
            continue
        endpoints = []
        for key in ("source", "target"):
            ep = tunnel.get(key) or {}
            if ep.get("drawer_id"):
                endpoints.append(ep["drawer_id"])
        appeared = any(
            any(eid in result_text for eid in endpoints)
            for _, result_text in recalls
        )
        cited = appeared and any(
            eid in final_assistant for eid in endpoints
        )
        log_tunnel_utility(
            rollout_id,
            tid,
            cited=bool(cited),
            query=recalls[-1][0] if recalls else "",
            wing=active_wing,
            metrics_dir=metrics_dir,
        )
        logged += 1
    return logged


def summarize_tunnel_metrics(metrics_dir: Path | None = None) -> dict[str, Any]:
    """Aggregate tunnel-utility log for kent doctor."""
    path = _metrics_path(metrics_dir)
    if not path.exists():
        return {"observations": 0, "citation_rate": 0.0, "by_tunnel": {}}
    total = 0
    cited = 0
    per_tunnel: dict[str, dict[str, int]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                total += 1
                tid = rec.get("tunnel_id", "?")
                bucket = per_tunnel.setdefault(tid, {"seen": 0, "cited": 0})
                bucket["seen"] += 1
                if rec.get("cited"):
                    cited += 1
                    bucket["cited"] += 1
    except OSError:
        return {"observations": 0, "citation_rate": 0.0, "by_tunnel": {}}
    return {
        "observations": total,
        "citation_rate": (cited / total) if total else 0.0,
        "by_tunnel": per_tunnel,
    }
