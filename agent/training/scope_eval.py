from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_scope_eval(
    palace_path: Path,
    kent_home: Path,
    critic_llm: Any,
    *,
    sample_size: int = 20,
    k: int = 5,
) -> dict[str, float]:
    """
    Game B: replay real queries at three scopes, critic picks best.
    Returns {'scope_accuracy': float}.
    """
    from mempalace.layers import Layer3
    from agent.memory.wings import list_wings

    layer3 = Layer3(palace_path)
    wings = list_wings(home=kent_home)
    if not wings:
        logger.warning("No wings found for Game B")
        return {"scope_accuracy": 0.0}

    hits = 0
    total = 0

    queries = _sample_queries(palace_path, sample_size)

    for query, expected_wing in queries:
        results_global = layer3.search(query, k=k)

        wing_results: dict[str, list] = {}
        for wing in wings[:5]:
            wing_results[wing] = layer3.search(query, k=k, wing=wing, room="daily")

        best_scope = await _critic_pick_scope(
            critic_llm, query, results_global, wing_results
        )

        if expected_wing and best_scope == expected_wing:
            hits += 1
        elif not expected_wing and best_scope == "global":
            hits += 1

        total += 1

    return {"scope_accuracy": hits / total if total > 0 else 0.0}


def _sample_queries(palace_path: Path, n: int) -> list[tuple[str, str]]:
    """Sample (query, expected_wing) pairs from palace metadata."""
    try:
        from mempalace.layers import MemoryStack

        stack = MemoryStack(palace_path)
        drawers = []
        for closet_id in stack.list_closets()[:50]:
            for d in stack.list_drawers(closet_id):
                drawers.append(d)
        if not drawers:
            return []
        sample = random.sample(drawers, min(n, len(drawers)))
        return [(d.get("content", "")[:100], d.get("wing", "")) for d in sample]
    except Exception:
        logger.warning("_sample_queries failed", exc_info=True)
        return []


async def _critic_pick_scope(
    critic_llm: Any,
    query: str,
    global_results: list,
    wing_results: dict[str, list],
) -> str:
    """Ask critic which scope produced the best results. Returns 'global' or a wing name."""
    from agent.events import TextDelta

    opts: dict[str, list] = {"global": global_results}
    opts.update(wing_results)

    desc = f"Query: {query}\n\n"
    for scope, results in opts.items():
        snippets = [r.get("content", "")[:80] for r in results[:3]]
        desc += f"Scope '{scope}': {'; '.join(snippets) or 'no results'}\n"
    desc += (
        "\nWhich scope label produced the most relevant results? "
        "Output only the scope label."
    )

    parts: list[str] = []
    async for ev in critic_llm.stream(
        [{"role": "user", "content": desc}],
        tools=[],
        system=None,
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)

    answer = "".join(parts).strip()
    if answer in opts:
        return answer
    return "global"
