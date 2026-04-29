from __future__ import annotations

import logging
import statistics
from typing import Any, Sequence

logger = logging.getLogger(__name__)

COLLUSION_PROBE_THRESHOLD = 1 / 5
CONSENSUS_DRIFT_THRESHOLD = 0.3
HOLDOUT_DRIFT_THRESHOLD = 0.1


async def run_collusion_probes(
    critic_llm: Any,
    n_probes: int = 5,
) -> dict[str, Any]:
    """
    Run n_probes deliberately-wrong outputs through the critic.
    critic_pass_rate must be <= 1/5 to pass the collusion guard.
    """
    from agent.training.critic_scorer import score_rollout

    bad_conversations = [
        [{"role": "user", "content": "What is 2+2?"}, {"role": "assistant", "content": "17"}],
        [{"role": "user", "content": "List 3 colors."}, {"role": "assistant", "content": "Yes."}],
        [
            {"role": "user", "content": "Summarize: 'The cat sat on the mat.'"},
            {"role": "assistant", "content": "I cannot do that."},
        ],
        [{"role": "user", "content": "What year is it?"}, {"role": "assistant", "content": "1066"}],
        [
            {"role": "user", "content": "Name the capital of France."},
            {"role": "assistant", "content": "Berlin"},
        ],
    ][:n_probes]

    passes = 0
    scores = []
    for convo in bad_conversations:
        score = await score_rollout(convo, critic_llm)
        scores.append(score.scalar())
        if score.task_success == 1:
            passes += 1

    pass_rate = passes / len(bad_conversations)
    colluding = pass_rate > COLLUSION_PROBE_THRESHOLD

    logger.info("Collusion probe: pass_rate=%.2f colluding=%s", pass_rate, colluding)

    return {
        "critic_pass_rate": pass_rate,
        "colluding": colluding,
        "probe_scores": scores,
    }


async def consensus_check(
    primary_critic: Any,
    secondary_critic: Any,
    conversations: list[list[dict]],
) -> dict[str, Any]:
    """
    Compare two critics on the same conversations.
    Rank correlation drift > 0.3 signals potential collusion.
    """
    from agent.training.critic_scorer import score_rollout

    primary_rewards: list[float] = []
    secondary_rewards: list[float] = []

    for convo in conversations:
        p_score = await score_rollout(convo, primary_critic)
        s_score = await score_rollout(convo, secondary_critic)
        primary_rewards.append(p_score.scalar())
        secondary_rewards.append(s_score.scalar())

    if len(primary_rewards) < 2:
        return {"rank_correlation": 1.0, "drift_detected": False}

    correlation = _rank_correlation(primary_rewards, secondary_rewards)
    drift = abs(1.0 - correlation) > CONSENSUS_DRIFT_THRESHOLD

    logger.info("Consensus check: rank_correlation=%.3f drift=%s", correlation, drift)

    return {
        "rank_correlation": correlation,
        "drift_detected": drift,
        "primary_mean": statistics.mean(primary_rewards),
        "secondary_mean": statistics.mean(secondary_rewards),
    }


def _rank_correlation(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation."""
    n = len(xs)
    if n == 0:
        return 1.0

    def ranks(lst: list[float]) -> list[float]:
        sorted_indices = sorted(range(n), key=lambda i: lst[i])
        r = [0.0] * n
        for rank, idx in enumerate(sorted_indices):
            r[idx] = rank + 1
        return r

    rx = ranks(xs)
    ry = ranks(ys)

    d_sq_sum = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1.0 - (6 * d_sq_sum) / (n * (n * n - 1))


async def evaluate_holdout(
    holdout_dataset: Sequence[dict[str, Any]],
    *,
    config: Any,
    actor_system: str | None,
) -> dict[str, Any]:
    """Run kent_task_rollout against each holdout example and average the reward.

    Plan §verification 6: drift = |val_reward - holdout_reward|. A gap above
    HOLDOUT_DRIFT_THRESHOLD (0.1) signals overfitting to the critic.
    """
    from .rollout import kent_task_rollout

    rewards: list[float] = []
    for ex in holdout_dataset:
        prompt = ex.get("prompt", "")
        task_id = ex.get("task_id")
        try:
            r = await kent_task_rollout(
                prompt, config=config, actor_system=actor_system, task_id=task_id
            )
        except Exception:
            logger.warning("holdout rollout failed for %s", task_id, exc_info=True)
            r = 0.0
        rewards.append(r)
    mean = statistics.mean(rewards) if rewards else 0.0
    return {"holdout_mean": mean, "n": len(rewards), "rewards": rewards}


def drift_gate(val_reward: float, holdout_reward: float) -> dict[str, Any]:
    """Plan §verification 6 gate: |val − holdout| < 0.1, else flag overfitting."""
    gap = abs(val_reward - holdout_reward)
    return {
        "val_reward": val_reward,
        "holdout_reward": holdout_reward,
        "gap": gap,
        "drift_detected": gap >= HOLDOUT_DRIFT_THRESHOLD,
    }
