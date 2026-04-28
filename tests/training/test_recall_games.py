"""Tests for recall games (Game A) against a mock palace."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.training.recall_games import run_recall_game_a, _ask_for_question


class _FakeLLM:
    context_window = 128_000

    async def stream(self, _messages, **_kw):
        from agent.events import TextDelta, AssistantMessageComplete, AssistantMessage
        yield TextDelta(text="What is the main topic here?")
        yield AssistantMessageComplete(message=AssistantMessage(content="What is the main topic here?", tool_calls=[]))

    def count_tokens(self, messages) -> int:
        return 5


async def test_ask_for_question_returns_string():
    llm = _FakeLLM()
    question = await _ask_for_question(llm, "The sky is blue because of Rayleigh scattering.")
    assert isinstance(question, str)
    assert len(question) > 0


async def test_recall_game_a_empty_palace(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    llm = _FakeLLM()

    with patch("mempalace.layers.MemoryStack") as MockStack:
        mock_stack = MagicMock()
        mock_stack.list_closets.return_value = []
        MockStack.return_value = mock_stack

        result = await run_recall_game_a(palace, llm)

    assert result["recall_a1"] == 0.0
    assert result["recall_a2"] == 0.0


async def test_recall_game_a_with_hit(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    llm = _FakeLLM()

    drawer_id = "drawer-123"

    with (
        patch("mempalace.layers.MemoryStack") as MockStack,
        patch("mempalace.layers.Layer3") as MockLayer3,
    ):
        mock_stack = MagicMock()
        mock_stack.list_closets.return_value = ["closet-1"]
        mock_stack.list_drawers.return_value = [
            {"id": drawer_id, "content": "Rayleigh scattering causes blue sky", "wing": "science"}
        ]
        MockStack.return_value = mock_stack

        mock_layer = MagicMock()
        mock_layer.search.return_value = [{"id": drawer_id, "content": "..."}]
        MockLayer3.return_value = mock_layer

        result = await run_recall_game_a(palace, llm, sample_size=1)

    assert result["recall_a1"] == pytest.approx(1.0)
