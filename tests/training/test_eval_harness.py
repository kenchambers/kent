"""Tests for eval_harness drift gate + consensus check (plan §verification 6)."""
from __future__ import annotations

import pytest

from agent.training.eval_harness import (
    drift_gate,
    HOLDOUT_DRIFT_THRESHOLD,
    _rank_correlation,
    consensus_check,
)


def test_drift_gate_below_threshold_clears():
    g = drift_gate(0.62, 0.59)
    assert g["drift_detected"] is False
    assert pytest.approx(g["gap"], 1e-9) == 0.03


def test_drift_gate_at_threshold_flagged():
    g = drift_gate(0.7, 0.6 - 1e-9)  # gap > HOLDOUT_DRIFT_THRESHOLD
    assert g["drift_detected"] is True
    assert g["gap"] >= HOLDOUT_DRIFT_THRESHOLD


def test_drift_gate_holdout_above_val_also_flagged():
    g = drift_gate(0.4, 0.55)
    assert g["drift_detected"] is True
    assert g["gap"] == pytest.approx(0.15, 1e-9)


def test_rank_correlation_perfect_agreement():
    xs = [0.1, 0.5, 0.9]
    assert _rank_correlation(xs, xs) == pytest.approx(1.0)


def test_rank_correlation_perfect_disagreement():
    xs = [0.1, 0.5, 0.9]
    ys = [0.9, 0.5, 0.1]
    assert _rank_correlation(xs, ys) == pytest.approx(-1.0)


class _DriftCritic:
    """Critic that scores the same conversations differently — used to
    exercise consensus_check when its raw rewards fall in different orders."""

    def __init__(self, scores: list[str]):
        self._scores = list(scores)
        self.context_window = 100_000

    async def stream(self, _messages, **_kw):
        from agent.events import TextDelta

        yield TextDelta(text=self._scores.pop(0))

    def count_tokens(self, _messages) -> int:
        return 1


async def test_consensus_check_no_drift_when_critics_agree():
    convos = [
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        for _ in range(3)
    ]
    primary_scores = [
        '{"task_success": 1, "reasoning_quality": 0.9, "tool_efficiency": 0.9, "memory_use": 0.9, "rationale": "good"}',
        '{"task_success": 0, "reasoning_quality": 0.1, "tool_efficiency": 0.1, "memory_use": 0.1, "rationale": "bad"}',
        '{"task_success": 1, "reasoning_quality": 0.5, "tool_efficiency": 0.5, "memory_use": 0.5, "rationale": "ok"}',
    ]
    secondary_scores = list(primary_scores)
    p = _DriftCritic(primary_scores)
    s = _DriftCritic(secondary_scores)
    out = await consensus_check(p, s, convos)
    assert out["drift_detected"] is False
    assert out["rank_correlation"] == pytest.approx(1.0)


async def test_consensus_check_flags_drift_on_inverse_ranking():
    convos = [
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        for _ in range(3)
    ]
    primary_scores = [
        '{"task_success": 1, "reasoning_quality": 0.9, "tool_efficiency": 0.9, "memory_use": 0.9, "rationale": "1"}',
        '{"task_success": 1, "reasoning_quality": 0.5, "tool_efficiency": 0.5, "memory_use": 0.5, "rationale": "2"}',
        '{"task_success": 0, "reasoning_quality": 0.1, "tool_efficiency": 0.1, "memory_use": 0.1, "rationale": "3"}',
    ]
    secondary_scores = [
        '{"task_success": 0, "reasoning_quality": 0.1, "tool_efficiency": 0.1, "memory_use": 0.1, "rationale": "1"}',
        '{"task_success": 1, "reasoning_quality": 0.5, "tool_efficiency": 0.5, "memory_use": 0.5, "rationale": "2"}',
        '{"task_success": 1, "reasoning_quality": 0.9, "tool_efficiency": 0.9, "memory_use": 0.9, "rationale": "3"}',
    ]
    p = _DriftCritic(primary_scores)
    s = _DriftCritic(secondary_scores)
    out = await consensus_check(p, s, convos)
    assert out["drift_detected"] is True
