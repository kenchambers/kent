from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from ._palace_api import list_drawers, search_drawers

logger = logging.getLogger(__name__)


async def run_scope_eval(
    palace_path: Path,
    kent_home: Path,
    critic_llm: Any,
    *,
    sample_size: int = 20,
    k: int = 5,
    paraphrase_llm: Any | None = None,
) -> dict[str, float]:
    """Game B: replay drawer-derived queries at three scopes (none / wing /
    wing+room=daily) and score whether the most-relevant scope matches the
    drawer's true wing.

    The previous implementation passed raw drawer content as the query, which
    made the "expected wing == drawer wing" label tautological. Here we use a
    paraphrasing LLM (defaults to the critic) to generate a natural-language
    *question*, then ask the critic which scope's results best answer it.
    A scope hit on the wing the drawer came from counts as a correct label.
    """
    from agent.memory.wings import list_wings

    wings = list_wings(home=kent_home)
    if not wings:
        logger.warning("Game B: no wings found")
        return {"scope_accuracy": 0.0, "samples": 0}

    drawers = list_drawers(palace_path, limit=max(sample_size * 4, 50))
    drawers = [d for d in drawers if d["wing"]]
    if not drawers:
        return {"scope_accuracy": 0.0, "samples": 0}

    sample = random.sample(drawers, min(sample_size, len(drawers)))
    paraphraser = paraphrase_llm or critic_llm

    hits = 0
    total = 0
    for drawer in sample:
        question = await _paraphrase_to_question(paraphraser, drawer["content"])
        if not question:
            continue

        results_global = search_drawers(palace_path, question, k=k)
        wing_results: dict[str, list[dict[str, Any]]] = {}
        for wing in wings[:5]:
            wing_results[wing] = search_drawers(
                palace_path, question, k=k, wing=wing, room="daily"
            )

        best_scope = await _critic_pick_scope(
            critic_llm, question, results_global, wing_results
        )

        expected = drawer["wing"]
        if best_scope == expected:
            hits += 1

        total += 1

    return {
        "scope_accuracy": hits / total if total else 0.0,
        "samples": total,
    }


async def _paraphrase_to_question(llm: Any, content: str) -> str:
    from agent.events import TextDelta

    parts: list[str] = []
    prompt = (
        f"Source content:\n{content[:500]}\n\n"
        "Rewrite as ONE natural-language question (under 25 words) a user might ask "
        "to retrieve this content. Do not quote the source verbatim. Output only the question."
    )
    try:
        async for ev in llm.stream(
            [{"role": "user", "content": prompt}], tools=[], system=None
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
    except Exception:
        return ""
    return "".join(parts).strip()


async def _critic_pick_scope(
    critic_llm: Any,
    query: str,
    global_results: list[dict[str, Any]],
    wing_results: dict[str, list[dict[str, Any]]],
) -> str:
    from agent.events import TextDelta

    opts: dict[str, list[dict[str, Any]]] = {"global": global_results}
    opts.update(wing_results)

    desc = f"Query: {query}\n\n"
    for scope, results in opts.items():
        snippets = [r["content"][:80] for r in results[:3] if r.get("content")]
        desc += f"Scope '{scope}': {'; '.join(snippets) or 'no results'}\n"
    desc += (
        "\nWhich scope label produced the most relevant results? "
        "Output only the scope label."
    )

    parts: list[str] = []
    try:
        async for ev in critic_llm.stream(
            [{"role": "user", "content": desc}], tools=[], system=None
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
    except Exception:
        return "global"

    answer = "".join(parts).strip()
    if answer in opts:
        return answer
    return "global"
