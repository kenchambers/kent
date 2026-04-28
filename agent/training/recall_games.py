from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_recall_game_a(
    palace_path: Path,
    actor_llm: Any,
    *,
    k: int = 5,
    sample_size: int = 20,
) -> dict[str, float]:
    """
    Game A: sample drawers -> ask actor to generate retrieval question -> score recall@k.
    Returns {'recall_a1': float, 'recall_a2': float}.

    A1 = unscoped global search.
    A2 = wing-scoped search when the drawer has a wing, else falls back to A1 result.
    """
    from mempalace.layers import MemoryStack, Layer3

    stack = MemoryStack(palace_path)
    closets = stack.list_closets()
    if not closets:
        logger.warning("No closets in palace for Game A")
        return {"recall_a1": 0.0, "recall_a2": 0.0}

    hits_a1 = 0
    hits_a2 = 0
    total = 0

    sample = random.sample(closets, min(sample_size, len(closets)))

    for closet_id in sample:
        drawers = stack.list_drawers(closet_id)
        if not drawers:
            continue
        drawer = random.choice(drawers)
        drawer_id = drawer["id"]
        drawer_content = drawer.get("content", "")
        wing = drawer.get("wing", "")

        question = await _ask_for_question(actor_llm, drawer_content)
        if not question:
            continue

        layer3 = Layer3(palace_path)

        # A1: unscoped
        results_a1 = layer3.search(question, k=k)
        ids_a1 = [r.get("id") for r in results_a1]
        if drawer_id in ids_a1:
            hits_a1 += 1

        # A2: wing-scoped when available
        if wing:
            results_a2 = layer3.search(question, k=k, wing=wing, room="daily")
            ids_a2 = [r.get("id") for r in results_a2]
            if drawer_id in ids_a2:
                hits_a2 += 1
        else:
            # No wing metadata — treat A2 same as A1
            if drawer_id in ids_a1:
                hits_a2 += 1

        total += 1

    if total == 0:
        return {"recall_a1": 0.0, "recall_a2": 0.0}

    return {
        "recall_a1": hits_a1 / total,
        "recall_a2": hits_a2 / total,
    }


async def _ask_for_question(actor_llm: Any, content: str) -> str:
    """Ask actor LLM: what question would retrieve this content?"""
    from agent.events import TextDelta

    parts: list[str] = []
    prompt = (
        f"Given this memory drawer content:\n\n{content}\n\n"
        "Write ONE short question (under 20 words) whose answer is in this content. "
        "Output only the question, nothing else."
    )
    async for ev in actor_llm.stream(
        [{"role": "user", "content": prompt}],
        tools=[],
        system=None,
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
    return "".join(parts).strip()
