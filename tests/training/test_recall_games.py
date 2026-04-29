"""Tests for recall games (Game A) using the new palace-API helpers."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.training.recall_games import run_recall_game_a, _ask_for_question


class _FakeLLM:
    context_window = 128_000

    def __init__(self, reply: str = "What is the main topic here?"):
        self._reply = reply

    async def stream(self, _messages, **_kw):
        from agent.events import TextDelta

        yield TextDelta(text=self._reply)

    def count_tokens(self, _messages) -> int:
        return 5


@pytest.mark.asyncio
async def test_ask_for_question_returns_string():
    llm = _FakeLLM()
    question = await _ask_for_question(
        llm, "The sky is blue because of Rayleigh scattering."
    )
    assert isinstance(question, str)
    assert len(question) > 0


@pytest.mark.asyncio
async def test_recall_game_a_empty_palace(tmp_path):
    with patch("agent.training.recall_games.list_drawers", return_value=[]):
        result = await run_recall_game_a(tmp_path / "palace", _FakeLLM())
    assert result["recall_a1"] == 0.0
    assert result["recall_a2"] == 0.0
    assert result["samples"] == 0


@pytest.mark.asyncio
async def test_recall_game_a_a1_hit_a2_unscoped_when_no_wing(tmp_path):
    drawer = {
        "id": "drawer_diary_AAA",
        "content": "Rayleigh scattering causes blue sky",
        "wing": "",  # no wing → A2 must be marked ineligible (a2 hit-rate 0)
        "room": "daily",
        "source": "diary",
        "metadata": {},
    }
    with (
        patch("agent.training.recall_games.list_drawers", return_value=[drawer]),
        patch(
            "agent.training.recall_games.search_drawers",
            return_value=[{"id": "drawer_diary_AAA", "content": "..."}],
        ),
    ):
        result = await run_recall_game_a(
            tmp_path / "palace", _FakeLLM(), sample_size=1
        )

    assert result["recall_a1"] == pytest.approx(1.0)
    assert result["recall_a2"] == pytest.approx(0.0)
    assert result["samples"] == 1


@pytest.mark.asyncio
async def test_recall_game_a_a2_hit_with_wing(tmp_path):
    drawer = {
        "id": "drawer_diary_BBB",
        "content": "Layer3.search latency is dominated by ChromaDB embedding lookup.",
        "wing": "main",
        "room": "daily",
        "source": "diary",
        "metadata": {},
    }

    def _search(_palace, _query, *, k, wing=None, room=None):
        # Both unscoped and wing-scoped find the drawer.
        return [{"id": "drawer_diary_BBB", "content": "x"}]

    with (
        patch(
            "agent.training.recall_games.list_drawers", return_value=[drawer]
        ),
        patch(
            "agent.training.recall_games.search_drawers", side_effect=_search
        ),
    ):
        result = await run_recall_game_a(
            tmp_path / "palace", _FakeLLM(), sample_size=1
        )

    assert result["recall_a1"] == pytest.approx(1.0)
    assert result["recall_a2"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_recall_game_a_miss_when_search_returns_other_drawer(tmp_path):
    drawer = {
        "id": "drawer_diary_TARGET",
        "content": "specific content",
        "wing": "main",
        "room": "daily",
        "source": "diary",
        "metadata": {},
    }
    with (
        patch(
            "agent.training.recall_games.list_drawers", return_value=[drawer]
        ),
        patch(
            "agent.training.recall_games.search_drawers",
            return_value=[{"id": "some_other_drawer"}],
        ),
    ):
        result = await run_recall_game_a(
            tmp_path / "palace", _FakeLLM(), sample_size=1
        )

    assert result["recall_a1"] == pytest.approx(0.0)
    assert result["recall_a2"] == pytest.approx(0.0)
