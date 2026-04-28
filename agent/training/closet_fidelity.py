from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_closet_fidelity(
    palace_path: Path,
    actor_llm: Any,
    *,
    sample_size: int = 10,
) -> dict[str, float]:
    """
    Game C: pick closet -> generate question -> can actor answer from closet alone?
    Returns {'closet_fidelity': float, 'diary_fidelity': float}.
    """
    from mempalace.layers import MemoryStack

    stack = MemoryStack(palace_path)
    closets = stack.list_closets()
    if not closets:
        return {"closet_fidelity": 0.0, "diary_fidelity": 0.0}

    hits_total = 0
    hits_diary = 0
    total = 0
    diary_total = 0

    sample = random.sample(closets, min(sample_size, len(closets)))

    for closet_id in sample:
        drawers = stack.list_drawers(closet_id)
        if not drawers:
            continue

        drawer = random.choice(drawers)
        content = drawer.get("content", "")
        source = drawer.get("source", "transcript")

        question = await _generate_question(actor_llm, content)
        if not question:
            continue

        closet_summary = stack.get_closet_summary(closet_id) or content
        answered = await _can_answer(actor_llm, question, closet_summary)

        if answered:
            hits_total += 1

        total += 1

        if source == "diary":
            diary_total += 1
            if answered:
                hits_diary += 1

    return {
        "closet_fidelity": hits_total / total if total > 0 else 0.0,
        "diary_fidelity": hits_diary / diary_total if diary_total > 0 else 0.0,
    }


async def _generate_question(actor_llm: Any, content: str) -> str:
    from agent.events import TextDelta

    parts: list[str] = []
    async for ev in actor_llm.stream(
        [{"role": "user", "content": (
            f"Content: {content[:300]}\n\n"
            "Write one question answered by this content. Output only the question."
        )}],
        tools=[],
        system=None,
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
    return "".join(parts).strip()


async def _can_answer(actor_llm: Any, question: str, context: str) -> bool:
    from agent.events import TextDelta

    parts: list[str] = []
    prompt = (
        f"Context:\n{context[:500]}\n\n"
        f"Question: {question}\n\n"
        "Answer YES if the context contains enough information to answer this question, "
        "NO otherwise. Output only YES or NO."
    )
    async for ev in actor_llm.stream(
        [{"role": "user", "content": prompt}],
        tools=[],
        system=None,
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
    answer = "".join(parts).strip().upper()
    return answer.startswith("YES")
