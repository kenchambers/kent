from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from ._palace_api import list_closets, list_drawers, classify_drawer

logger = logging.getLogger(__name__)


async def run_closet_fidelity(
    palace_path: Path,
    actor_llm: Any,
    *,
    sample_size: int = 10,
) -> dict[str, float]:
    """Game C: pick a closet → generate question from a member drawer →
    can the actor answer from the closet summary alone?

    Returns recall numbers stratified by drawer source so we can detect
    diary-summary regressions distinctly from transcript-summary regressions.
    """
    closets = list_closets(palace_path, limit=200)
    if not closets:
        return {
            "closet_fidelity": 0.0,
            "diary_fidelity": 0.0,
            "transcript_fidelity": 0.0,
            "samples": 0,
        }

    # Index drawers by source_file so we can pair a closet to candidate drawers.
    drawers = list_drawers(palace_path, limit=2000)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for d in drawers:
        sf = d["metadata"].get("source_file") or ""
        by_source.setdefault(sf, []).append(d)

    sample = random.sample(closets, min(sample_size, len(closets)))
    hits_total = 0
    hits_by_source = {"diary": 0, "transcript": 0}
    seen_by_source = {"diary": 0, "transcript": 0}
    total = 0

    for closet in sample:
        closet_text = closet["content"]
        source_file = closet["metadata"].get("source_file") or ""
        candidates = by_source.get(source_file, [])
        if not candidates:
            continue
        drawer = random.choice(candidates)
        drawer_content = drawer["content"]
        if not drawer_content:
            continue

        question = await _generate_question(actor_llm, drawer_content)
        if not question:
            continue

        answered = await _can_answer(actor_llm, question, closet_text)

        total += 1
        if answered:
            hits_total += 1

        source = classify_drawer(drawer["id"], drawer["metadata"])
        if source in seen_by_source:
            seen_by_source[source] += 1
            if answered:
                hits_by_source[source] += 1

    if total == 0:
        return {
            "closet_fidelity": 0.0,
            "diary_fidelity": 0.0,
            "transcript_fidelity": 0.0,
            "samples": 0,
        }

    def _safe_div(num: int, den: int) -> float:
        return (num / den) if den else 0.0

    return {
        "closet_fidelity": hits_total / total,
        "diary_fidelity": _safe_div(hits_by_source["diary"], seen_by_source["diary"]),
        "transcript_fidelity": _safe_div(
            hits_by_source["transcript"], seen_by_source["transcript"]
        ),
        "samples": total,
        "diary_samples": seen_by_source["diary"],
        "transcript_samples": seen_by_source["transcript"],
    }


async def _generate_question(actor_llm: Any, content: str) -> str:
    from agent.events import TextDelta

    parts: list[str] = []
    try:
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
    except Exception:
        return ""
    return "".join(parts).strip()


async def _can_answer(actor_llm: Any, question: str, context: str) -> bool:
    from agent.events import TextDelta

    parts: list[str] = []
    prompt = (
        f"Context:\n{context[:800]}\n\n"
        f"Question: {question}\n\n"
        "Answer YES if the context contains enough information to answer this question, "
        "NO otherwise. Output only YES or NO."
    )
    try:
        async for ev in actor_llm.stream(
            [{"role": "user", "content": prompt}], tools=[], system=None
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
    except Exception:
        return False
    return "".join(parts).strip().upper().startswith("YES")
