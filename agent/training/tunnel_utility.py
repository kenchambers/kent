from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_METRICS_DIR = Path.home() / ".kent" / "lightning_store" / "metrics"


def log_tunnel_utility(
    rollout_id: str,
    tunnel_id: str,
    cited: bool,
    query: str = "",
) -> None:
    """Log whether a followed tunnel yielded cited content."""
    _METRICS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "rollout_id": rollout_id,
        "tunnel_id": tunnel_id,
        "cited": cited,
        "query": query,
    }
    path = _METRICS_DIR / "tunnel_utility.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    logger.debug("tunnel_utility logged: %s cited=%s", tunnel_id, cited)
