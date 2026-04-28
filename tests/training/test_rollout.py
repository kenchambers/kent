"""Integration smoke test for kent_task_rollout using FakeLLM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent.training import TrainingConfig


class FakeLLM:
    """Deterministic fake LLM that always responds with 'done'."""

    context_window = 128_000

    async def stream(self, _messages, _tools=None, _system=None, **_kw):
        from agent.events import (
            TextDelta,
            AssistantMessageComplete,
            AssistantMessage,
        )
        yield TextDelta(text="done")
        yield AssistantMessageComplete(
            message=AssistantMessage(content="done", tool_calls=[])
        )

    def count_tokens(self, _messages) -> int:
        return 10


def _make_config(tmp_path: Path) -> TrainingConfig:
    kent_home = tmp_path / ".kent"
    palace = kent_home / "palace"
    palace.mkdir(parents=True)
    # Plan-mandated: SQLite must be a copy, not a hardlink. Without an
    # actual chroma.sqlite3 in the source palace the isolation self-check
    # never exercises that branch.
    (palace / "chroma.sqlite3").write_bytes(b"sqlite header bytes")
    (kent_home / "diaries" / "main").mkdir(parents=True)
    (kent_home / "active_wing.txt").write_text("main\n")
    return TrainingConfig(
        actor_base_url="http://fake",
        actor_api_key="fake",
        actor_model="fake-model",
        actor_family="fake",
        critic_base_url="http://fake",
        critic_api_key="fake",
        critic_model="fake-critic",
        critic_family="fake-critic",
        palace_path=palace,
        kent_home=kent_home,
        n_runners=1,
        max_rounds=2,
        train_size=2,
    )


@pytest.mark.integration
async def test_rollout_runs_end_to_end(tmp_path):
    """Rollout completes, reward in [0,1], scratch palace is cleaned up."""
    config = _make_config(tmp_path)
    fake_llm = FakeLLM()

    captured: dict = {}

    async def fake_score_rollout(messages, _critic_llm):
        from agent.training.critic_scorer import CriticScore
        captured["transcript"] = list(messages)
        return CriticScore(
            task_success=1,
            reasoning_quality=0.8,
            tool_efficiency=0.7,
            memory_use=0.6,
            rationale="good",
        )

    with (
        patch("agent.llm.OpenAICompatibleLLM", return_value=fake_llm),
        patch("agent.training.rollout.score_rollout", side_effect=fake_score_rollout),
    ):
        from agent.training.rollout import kent_task_rollout
        reward = await kent_task_rollout(
            "What do you remember?", config=config, task_id="test-001"
        )

    assert 0.0 <= reward <= 1.0
    transcript = captured.get("transcript", [])
    assert len(transcript) >= 2, (
        "critic should see user prompt + assistant response, not just the prompt"
    )
    assert any(m.get("role") == "assistant" for m in transcript)


@pytest.mark.integration
async def test_rollout_scratch_cleaned_up(tmp_path):
    """Scratch palace directory is removed after rollout completes."""
    config = _make_config(tmp_path)
    fake_llm = FakeLLM()

    created_scratches: list[Path] = []

    original_snapshot = __import__(
        "agent.training.palace_isolation", fromlist=["snapshot"]
    ).snapshot

    def tracked_snapshot(kent_home, palace_path):
        rid, scratch = original_snapshot(kent_home, palace_path)
        created_scratches.append(scratch)
        return rid, scratch

    async def fake_score_rollout(_messages, _critic_llm):
        from agent.training.critic_scorer import CriticScore
        return CriticScore(1, 0.8, 0.7, 0.6, "good")

    with (
        patch("agent.llm.OpenAICompatibleLLM", return_value=fake_llm),
        patch("agent.training.rollout.snapshot", side_effect=tracked_snapshot),
        patch("agent.training.rollout.score_rollout", side_effect=fake_score_rollout),
    ):
        from agent.training.rollout import kent_task_rollout
        await kent_task_rollout("hello", config=config, task_id="cleanup-test")

    for scratch in created_scratches:
        assert not scratch.exists(), f"Scratch not cleaned up: {scratch}"


@pytest.mark.integration
async def test_rollout_returns_zero_on_failure(tmp_path):
    """Rollout returns 0.0 when actor raises an exception."""
    config = _make_config(tmp_path)

    with patch("agent.llm.OpenAICompatibleLLM", side_effect=RuntimeError("boom")):
        from agent.training.rollout import kent_task_rollout
        reward = await kent_task_rollout("hello", config=config)

    assert reward == 0.0
