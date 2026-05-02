from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from .palace_isolation import snapshot, cleanup, assert_isolation
from .critic_scorer import score_rollout
from .tunnel_utility import log_rollout_tunnel_observations
from . import TrainingConfig

logger = logging.getLogger(__name__)

# @agl.rollout fixes the function signature to (task, prompt_template, rollout).
# Non-task transient state (LLM endpoints, scratch paths) lives here so the
# rollout can read it without mutating the trainer-mandated signature.
_active_config: TrainingConfig | None = None
_config_lock = threading.Lock()


def set_active_config(config: TrainingConfig | None) -> None:
    global _active_config
    with _config_lock:
        _active_config = config


def get_active_config() -> TrainingConfig:
    with _config_lock:
        if _active_config is None:
            raise RuntimeError(
                "training config not set; call set_active_config(cfg) before running rollouts"
            )
        return _active_config


def _load_frozen_resources(lightning_store: Path, exclude: str) -> dict[str, str]:
    """Load previously optimized resources (excluding the one being trained now).

    Plan line 47: "Each round freezes prior resources." Frozen resources are read
    from lightning_store/resources/<name>.txt and threaded into the rollout
    system prompt so the actor experiences earlier optimization.
    """
    resources_dir = lightning_store / "resources"
    if not resources_dir.exists():
        return {}
    out: dict[str, str] = {}
    for path in resources_dir.glob("*.txt"):
        name = path.stem
        if name == exclude:
            continue
        try:
            out[name] = path.read_text(encoding="utf-8")
        except Exception:
            logger.debug("failed to read frozen resource %s", path, exc_info=True)
    return out


async def _run_rollout_impl(
    prompt: str,
    *,
    config: TrainingConfig,
    actor_system: str | None = None,
    task_id: str | None = None,
) -> tuple[float, list[dict], str | None]:
    """Run one rollout. Returns (reward, transcript, rationale)."""
    from agent.llm import OpenAICompatibleLLM
    from agent.memory.mempalace_store import MemPalaceStore
    from agent.builtin import (
        Spawn,
        MemoryRecall,
        MemoryRecallHere,
        DiaryWrite,
        SetWing,
        TaskStart,
        TaskEnd,
        CodeDrawer,
        ClosetRefresh,
    )
    from agent.builtin.tunnel_create import TunnelCreate
    from agent.loop import run as loop_run
    from agent.cli import build_registry, _build_system_prompt
    from agent.events import AssistantMessageComplete, ToolResult as ToolResultEv

    kent_home: Path = (
        config.kent_home if config.kent_home is not None else Path.home() / ".kent"
    )
    palace_path: Path = (
        config.palace_path if config.palace_path is not None else kent_home / "palace"
    )

    rollout_internal_id: str | None = None
    try:
        rollout_internal_id, scratch_root = snapshot(kent_home, palace_path)
        assert_isolation(kent_home, palace_path, scratch_root)

        scratch_palace = scratch_root / "palace"

        actor_llm = OpenAICompatibleLLM(
            base_url=config.actor_base_url,
            api_key=config.actor_api_key,
            model=config.actor_model,
        )
        critic_llm = OpenAICompatibleLLM(
            base_url=config.critic_base_url,
            api_key=config.critic_api_key,
            model=config.critic_model,
        )

        memory_store = MemPalaceStore(
            palace_path=scratch_palace,
            kent_home=scratch_root,
        )

        registry = build_registry()
        registry.register(Spawn(parent_registry=registry, llm=actor_llm, memory_store=memory_store))  # type: ignore[arg-type]
        registry.register(MemoryRecall(memory_store))  # type: ignore[arg-type]
        registry.register(MemoryRecallHere(memory_store))  # type: ignore[arg-type]
        registry.register(DiaryWrite(memory_store))  # type: ignore[arg-type]
        registry.register(SetWing(memory_store))  # type: ignore[arg-type]
        registry.register(TunnelCreate())  # type: ignore[arg-type]
        registry.register(TaskStart())  # type: ignore[arg-type]
        registry.register(TaskEnd())  # type: ignore[arg-type]
        registry.register(CodeDrawer(scratch_palace, memory_store.active_wing))  # type: ignore[arg-type]
        registry.register(ClosetRefresh(  # type: ignore[arg-type]
            scratch_palace, memory_store.active_wing,
            base_url=config.actor_base_url,
            api_key=config.actor_api_key,
            model=config.actor_model,
        ))

        system = actor_system if actor_system else _build_system_prompt(memory_store)

        messages: list[dict] = [{"role": "user", "content": prompt}]
        collected: list[dict] = list(messages)

        async for ev in loop_run(
            messages=messages,
            tools=registry,
            llm=actor_llm,
            system=system,
            memory_store=memory_store,
            max_turns=config.max_rounds,
        ):
            if isinstance(ev, AssistantMessageComplete):
                collected.append(ev.message.to_openai_dict())
            elif isinstance(ev, ToolResultEv):
                collected.append(ev.to_message())

        score = await score_rollout(collected, critic_llm)
        reward = score.scalar()
        logger.info(
            "rollout %s reward=%.3f", task_id or rollout_internal_id, reward
        )

        try:
            metrics_dir = config.lightning_store / "metrics"
            log_rollout_tunnel_observations(
                task_id or rollout_internal_id or "",
                collected,
                active_wing=memory_store.active_wing,
                metrics_dir=metrics_dir,
            )
        except Exception:
            logger.debug("tunnel-utility logging failed", exc_info=True)

        return reward, collected, score.rationale

    except Exception:
        logger.exception("rollout %s failed", task_id or rollout_internal_id)
        return 0.0, [], None
    finally:
        if rollout_internal_id is not None:
            cleanup(rollout_internal_id)


async def kent_task_rollout(
    prompt: str,
    *,
    config: TrainingConfig,
    actor_system: str | None = None,
    task_id: str | None = None,
) -> float:
    """Direct (non-APO) rollout entry — used by tests and ad-hoc evaluation."""
    reward, _, _ = await _run_rollout_impl(
        prompt, config=config, actor_system=actor_system, task_id=task_id
    )
    return reward


def build_apo_agent():
    """Build the APO LitAgent. Imports agentlightning lazily so callers that
    don't exercise APO don't pull in the optional `poml` dependency."""
    import agentlightning as agl

    @agl.rollout
    async def apo_kent_rollout(
        task: Any, prompt_template: agl.PromptTemplate, rollout: agl.Rollout
    ) -> float:
        import time as _time
        _t0 = _time.monotonic()
        config = get_active_config()
        if isinstance(task, dict):
            prompt = task.get("prompt", "")
            task_id = task.get("task_id")
        else:
            prompt = str(task)
            task_id = None
        logger.info("[apo-rollout] start task_id=%s prompt=%r", task_id, prompt[:60])

        frozen = _load_frozen_resources(
            config.lightning_store, exclude=getattr(config, "current_resource", "")
        )
        frozen_block = "\n\n".join(f"[{k}]\n{v}" for k, v in frozen.items() if v)
        actor_system = prompt_template.template or ""
        if frozen_block:
            actor_system = f"{actor_system}\n\n{frozen_block}".strip()

        reward, _, rationale = await _run_rollout_impl(
            prompt,
            config=config,
            actor_system=actor_system or None,
            task_id=task_id or getattr(rollout, "rollout_id", None),
        )

        try:
            agl.emit_object(
                {"feedback_text": rationale or "", "rollout_id": task_id or ""}
            )
            agl.emit_reward(reward)
        except Exception:
            logger.debug("agl emission failed", exc_info=True)

        logger.info(
            "[apo-rollout] done task_id=%s reward=%.3f elapsed=%.1fs",
            task_id, reward, _time.monotonic() - _t0,
        )
        return reward

    return apo_kent_rollout


def build_recall_game_apo_agent():
    """LitAgent for Game-A-style APO training of `query_rewrite_policy`.

    Per plan line 25: "sample drawer → ask actor 'what question would retrieve
    this?' → run Layer3.search → reward = recall@5 (drawer ID hit)". This
    rollout shape is one LLM call + one vector search, so each rollout takes
    ~10s (vs ~30-60s for the full kent agent loop). The reward signal —
    embedding similarity between the actor's generated query and the source
    drawer — is well-defined and continuous, which is exactly what APO's
    textual-gradient step needs.

    Each task dict: {"drawer_text": str, "palace_path": str}.
    """
    import agentlightning as agl

    @agl.rollout
    async def recall_game_rollout(
        task: Any, prompt_template: agl.PromptTemplate, rollout: agl.Rollout
    ) -> float:
        import time as _time
        from agent.llm import OpenAICompatibleLLM
        from agent.events import TextDelta
        from mempalace.layers import Layer3

        _t0 = _time.monotonic()
        config = get_active_config()

        if isinstance(task, dict):
            drawer_text = str(task.get("drawer_text", ""))
            palace_path = str(task.get("palace_path", ""))
            task_wing = task.get("wing") or None
            task_room = task.get("room") or None
        else:
            drawer_text = str(task)
            palace_path = ""
            task_wing = None
            task_room = None

        instruction = (prompt_template.template or "").strip() or (
            "Phrase a question whose answer is in this drawer content."
        )
        prompt = (
            f"{instruction}\n\n"
            f"Drawer content:\n{drawer_text[:600]}\n\n"
            "Output one short retrieval query, no preamble."
        )

        actor_llm = OpenAICompatibleLLM(
            base_url=config.actor_base_url,
            api_key=config.actor_api_key,
            model=config.actor_model,
        )

        parts: list[str] = []
        try:
            async for ev in actor_llm.stream(
                [{"role": "user", "content": prompt}],
                tools=[],
                system=None,
            ):
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
        except Exception:
            logger.exception("recall-game actor call failed")
            try:
                agl.emit_reward(0.0)
            except Exception:
                pass
            return 0.0

        query = "".join(parts).strip().split("\n")[0][:200]
        if not query or not palace_path:
            try:
                agl.emit_reward(0.0)
            except Exception:
                pass
            return 0.0

        try:
            layer3 = Layer3(palace_path)
            search_kwargs: dict = {"n_results": 1}
            if task_wing:
                search_kwargs["wing"] = task_wing
            if task_room:
                search_kwargs["room"] = task_room
            results = layer3.search_raw(query, **search_kwargs) or []
            similarity = float(results[0].get("similarity", 0.0)) if results else 0.0
            # Map [-1, 1] cosine similarity into [0, 1] for APO.
            reward = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
        except Exception:
            logger.exception("recall-game search failed")
            reward = 0.0

        try:
            agl.emit_object({"query": query, "reward": reward})
            agl.emit_reward(reward)
        except Exception:
            logger.debug("agl emission failed", exc_info=True)

        logger.info(
            "[recall-rollout] query=%r reward=%.3f elapsed=%.1fs",
            query, reward, _time.monotonic() - _t0,
        )
        return reward

    return recall_game_rollout


def build_code_query_apo_agent():
    """LitAgent for APO training of `code_query_policy`.

    Task dict: {"drawer_text": str, "palace_path": str, "wing": str|None, "room": str|None}.
    Samples drawers where room=="code" (or source_label=="code"). Reward: cosine sim of
    generated query → source code drawer via Layer3.search_raw(room="code", n_results=1).
    """
    import agentlightning as agl

    @agl.rollout
    async def code_query_rollout(
        task: Any, prompt_template: agl.PromptTemplate, rollout: agl.Rollout
    ) -> float:
        import time as _time
        from agent.llm import OpenAICompatibleLLM
        from agent.events import TextDelta
        from mempalace.layers import Layer3

        _t0 = _time.monotonic()
        config = get_active_config()

        if isinstance(task, dict):
            drawer_text = str(task.get("drawer_text", ""))
            palace_path = str(task.get("palace_path", ""))
            task_wing = task.get("wing") or None
        else:
            drawer_text = str(task)
            palace_path = ""
            task_wing = None

        instruction = (prompt_template.template or "").strip() or (
            "Phrase a question to retrieve this code drawer: name the symbol, file, "
            "or behavior the snippet implements."
        )
        prompt = (
            f"{instruction}\n\n"
            f"Code drawer content:\n{drawer_text[:600]}\n\n"
            "Output one short retrieval query, no preamble."
        )

        actor_llm = OpenAICompatibleLLM(
            base_url=config.actor_base_url,
            api_key=config.actor_api_key,
            model=config.actor_model,
        )

        parts: list[str] = []
        try:
            async for ev in actor_llm.stream(
                [{"role": "user", "content": prompt}],
                tools=[],
                system=None,
            ):
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
        except Exception:
            logger.exception("code-query actor call failed")
            try:
                agl.emit_reward(0.0)
            except Exception:
                pass
            return 0.0

        query = "".join(parts).strip().split("\n")[0][:200]
        if not query or not palace_path:
            try:
                agl.emit_reward(0.0)
            except Exception:
                pass
            return 0.0

        try:
            layer3 = Layer3(palace_path)
            search_kwargs: dict = {"n_results": 1, "room": "code"}
            if task_wing:
                search_kwargs["wing"] = task_wing
            results = layer3.search_raw(query, **search_kwargs) or []
            similarity = float(results[0].get("similarity", 0.0)) if results else 0.0
            reward = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
        except Exception:
            logger.exception("code-query search failed")
            reward = 0.0

        try:
            agl.emit_object({"query": query, "reward": reward})
            agl.emit_reward(reward)
        except Exception:
            logger.debug("agl emission failed", exc_info=True)

        logger.info(
            "[code-query-rollout] query=%r reward=%.3f elapsed=%.1fs",
            query, reward, _time.monotonic() - _t0,
        )
        return reward

    return code_query_rollout
