from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from ._palace_api import list_drawers, search_drawers

logger = logging.getLogger(__name__)


async def run_recall_game_a(
    palace_path: Path,
    actor_llm: Any,
    *,
    k: int = 5,
    sample_size: int = 20,
) -> dict[str, float]:
    """Game A: sample drawers → ask actor for retrieval question → score recall@k.

    A1 = unscoped global search (trains query_rewrite_policy without wing constraint).
    A2 = wing-scoped search when the drawer carries a wing (mirrors recall_in_wing).
    """
    drawers = list_drawers(palace_path, limit=max(sample_size * 4, 50))
    if not drawers:
        logger.warning("Game A: palace has no drawers")
        return {"recall_a1": 0.0, "recall_a2": 0.0, "samples": 0}

    sample = random.sample(drawers, min(sample_size, len(drawers)))
    hits_a1 = 0
    hits_a2 = 0
    eligible_a2 = 0
    total = 0

    for drawer in sample:
        content = drawer["content"]
        if not content:
            continue
        question = await _ask_for_question(actor_llm, content)
        if not question:
            continue

        results_a1 = search_drawers(palace_path, question, k=k)
        ids_a1 = {r["id"] for r in results_a1}
        if drawer["id"] in ids_a1:
            hits_a1 += 1

        wing = drawer["wing"]
        if wing:
            eligible_a2 += 1
            results_a2 = search_drawers(
                palace_path, question, k=k, wing=wing, room="daily"
            )
            ids_a2 = {r["id"] for r in results_a2}
            if drawer["id"] in ids_a2:
                hits_a2 += 1

        total += 1

    if total == 0:
        return {"recall_a1": 0.0, "recall_a2": 0.0, "samples": 0}

    return {
        "recall_a1": hits_a1 / total,
        "recall_a2": (hits_a2 / eligible_a2) if eligible_a2 else 0.0,
        "samples": total,
    }


async def _ask_for_question(actor_llm: Any, content: str) -> str:
    from agent.events import TextDelta

    parts: list[str] = []
    prompt = (
        f"Given this memory drawer content:\n\n{content[:600]}\n\n"
        "Write ONE short question (under 20 words) whose answer is in this content. "
        "Output only the question, nothing else."
    )
    try:
        async for ev in actor_llm.stream(
            [{"role": "user", "content": prompt}], tools=[], system=None
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
    except Exception:
        logger.debug("actor_llm.stream failed in _ask_for_question", exc_info=True)
        return ""
    return "".join(parts).strip()
