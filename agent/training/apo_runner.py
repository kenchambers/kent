from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

RESOURCE_ORDER = [
    "actor_system_prompt",
    "retrieval_policy",
    "scope_policy",
    "query_rewrite_policy",
    "closet_summary_policy",
]

# actor_system_prompt's baseline resolves at call time from cli._SYSTEM_PROMPT_BASE
# so we don't pin a stale copy here.
RESOURCE_BASELINES: dict[str, str] = {
    "actor_system_prompt": "",
    "retrieval_policy": (
        "When to call memory_recall: call it when the user asks about past conversations, "
        "facts you recorded, or anything that might be in long-term memory."
    ),
    "scope_policy": (
        "Decide search scope: use memory_recall for global search, memory_recall_here for "
        "wing-specific search when context is clearly wing-local."
    ),
    "query_rewrite_policy": (
        "Phrase a question to retrieve this drawer: use specific nouns and verbs from the "
        "drawer content as search terms."
    ),
    "closet_summary_policy": (
        "When writing a closet, preserve answerability of: ensure each key fact or decision "
        "can be retrieved by its natural question form."
    ),
    "critic_rubric": "task_success / reasoning / tool_eff / memory_use",
}


def _resource_baseline(name: str) -> str:
    """Resolve seed prompt; defer actor_system_prompt to cli to avoid pinning a stale copy."""
    if name == "actor_system_prompt":
        try:
            from agent.cli import _SYSTEM_PROMPT_BASE
            return _SYSTEM_PROMPT_BASE
        except Exception:
            logger.warning("could not import _SYSTEM_PROMPT_BASE; using empty baseline")
            return ""
    return RESOURCE_BASELINES.get(name, "")


def train_resource(
    resource_name: str,
    *,
    train_dataset: Sequence[Any],
    val_dataset: Sequence[Any],
    store_path: Path,
    n_rounds: int = 3,
    n_runners: int = 4,
    openai_api_key: str,
    apo_base_url: str | None = None,
    apo_request_timeout: float = 120.0,
    gradient_model: str = "gpt-5-mini",
    apply_edit_model: str = "gpt-4.1-mini",
    beam_width: int = 4,
    branch_factor: int = 4,
    gradient_batch_size: int = 4,
    val_batch_size: int = 16,
    rollout_batch_timeout: float = 600.0,
    agent_builder: Any = None,
) -> dict[str, Any]:
    """Run APO training for one resource. Returns metrics dict.

    Synchronous: agentlightning.Trainer.fit is sync; do not call from inside an
    asyncio event loop.

    apo_base_url: override the OpenAI endpoint (e.g. point at Atlas Cloud).
        Plan line 140 acknowledges APO depends on AsyncOpenAI; an OpenAI-compatible
        endpoint works as a substitute.
    """
    import agentlightning as agl
    from openai import AsyncOpenAI

    from .rollout import build_apo_agent, set_active_config

    store_path.mkdir(parents=True, exist_ok=True)
    resources_path = store_path / "resources"
    resources_path.mkdir(exist_ok=True)

    baseline = _resource_baseline(resource_name)
    prompt_template = agl.PromptTemplate(template=baseline, engine="f-string")

    # Per-request timeout on the AsyncOpenAI client prevents APO's textual-
    # gradient and apply_edit calls from hanging indefinitely on a slow or
    # non-responsive endpoint. Defaults to 120s; tune via apo_request_timeout.
    client_kwargs: dict[str, Any] = {
        "api_key": openai_api_key,
        "timeout": apo_request_timeout,
    }
    if apo_base_url:
        client_kwargs["base_url"] = apo_base_url

    algorithm = agl.APO(
        AsyncOpenAI(**client_kwargs),
        gradient_model=gradient_model,
        apply_edit_model=apply_edit_model,
        beam_rounds=n_rounds,
        beam_width=beam_width,
        branch_factor=branch_factor,
        gradient_batch_size=gradient_batch_size,
        val_batch_size=val_batch_size,
        rollout_batch_timeout=rollout_batch_timeout,
    )

    # SharedMemoryExecutionStrategy keeps everything in one process; the default
    # ClientServerExecutionStrategy spawns subprocesses and trips a pickling
    # error on the locally-defined runner function (macOS spawn semantics).
    # TraceToMessages adapter is required by APO (default adapter is incompatible).
    trainer = agl.Trainer(
        n_runners=n_runners,
        algorithm=algorithm,
        initial_resources={resource_name: prompt_template},
        strategy=agl.SharedMemoryExecutionStrategy(n_runners=n_runners),
        adapter=agl.TraceToMessages(),
    )

    agent = agent_builder() if agent_builder is not None else build_apo_agent()

    trainer.fit(agent, list(train_dataset), val_dataset=list(val_dataset))

    best_template = algorithm.get_best_prompt()
    val_reward = float(getattr(algorithm, "_history_best_score", 0.0) or 0.0)

    out_path = resources_path / f"{resource_name}.txt"
    out_path.write_text(best_template.template, encoding="utf-8")
    logger.info(
        "Saved optimized %s to %s (val_reward=%.3f)",
        resource_name,
        out_path,
        val_reward,
    )

    set_active_config(None)

    return {
        "resource": resource_name,
        "val_reward": val_reward,
        "rounds": n_rounds,
        "output_path": str(out_path),
    }
