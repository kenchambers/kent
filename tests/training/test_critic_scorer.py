"""Tests for critic scoring: JSON parsing, scalar reward math, parse error fallback."""
from __future__ import annotations

import pytest

from agent.training.critic_scorer import CriticScore, _parse_score, _format_transcript


def test_scalar_reward_weights():
    score = CriticScore(
        task_success=1,
        reasoning_quality=1.0,
        tool_efficiency=1.0,
        memory_use=1.0,
        rationale="perfect",
    )
    assert score.scalar() == pytest.approx(1.0)


def test_scalar_reward_zero():
    score = CriticScore(
        task_success=0,
        reasoning_quality=0.0,
        tool_efficiency=0.0,
        memory_use=0.0,
        rationale="fail",
    )
    assert score.scalar() == pytest.approx(0.0)


def test_scalar_reward_partial():
    score = CriticScore(
        task_success=1,
        reasoning_quality=0.0,
        tool_efficiency=0.0,
        memory_use=0.0,
        rationale="partial",
    )
    assert score.scalar() == pytest.approx(0.5)


def test_parse_score_valid_json():
    raw = '{"task_success": 1, "reasoning_quality": 0.8, "tool_efficiency": 0.6, "memory_use": 0.7, "rationale": "good"}'
    score = _parse_score(raw)
    assert score.task_success == 1
    assert score.reasoning_quality == pytest.approx(0.8)
    assert score.rationale == "good"


def test_parse_score_with_code_fence():
    raw = '```json\n{"task_success": 0, "reasoning_quality": 0.5, "tool_efficiency": 0.5, "memory_use": 0.5, "rationale": "ok"}\n```'
    score = _parse_score(raw)
    assert score.task_success == 0
    assert score.rationale == "ok"


def test_parse_score_fallback_on_bad_json():
    score = _parse_score("this is not json at all")
    assert score.task_success == 0
    assert score.reasoning_quality == pytest.approx(0.0)
    assert score.rationale == "parse_error"


def test_parse_score_missing_fields_use_defaults():
    score = _parse_score('{"task_success": 1}')
    assert score.task_success == 1
    assert score.reasoning_quality == pytest.approx(0.5)


def test_format_transcript_basic():
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    transcript = _format_transcript(messages)
    assert "[USER] Hello" in transcript
    assert "[ASSISTANT] Hi there" in transcript


def test_format_transcript_list_content():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
    ]
    transcript = _format_transcript(messages)
    assert "Hello" in transcript
