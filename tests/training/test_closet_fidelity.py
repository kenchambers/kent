"""Game C diary stratification — make sure diary vs transcript drawers
contribute to the right buckets."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.training.closet_fidelity import run_closet_fidelity


class _AlwaysYes:
    context_window = 100_000

    async def stream(self, _messages, **_kw):
        from agent.events import TextDelta

        yield TextDelta(text="YES")

    def count_tokens(self, _messages) -> int:
        return 5


@pytest.mark.asyncio
async def test_diary_fidelity_separates_from_transcript_fidelity(tmp_path):
    closets = [
        {
            "id": "closet_1",
            "content": "summary one",
            "wing": "main",
            "room": "daily",
            "metadata": {"source_file": "/diaries/main/2026-04-29.md"},
        },
        {
            "id": "closet_2",
            "content": "summary two",
            "wing": "main",
            "room": "general",
            "metadata": {"source_file": "/transcripts/sess.jsonl"},
        },
    ]
    drawers = [
        {
            "id": "drawer_diary_AAA",
            "content": "Project pinned to mempalace>=3.3.",
            "wing": "main",
            "room": "daily",
            "source": "diary",
            "metadata": {
                "source_file": "/diaries/main/2026-04-29.md",
                "source_session": "daily_diary",
            },
        },
        {
            "id": "sweep_BBB",
            "content": "User said hi.",
            "wing": "main",
            "room": "general",
            "source": "transcript",
            "metadata": {
                "source_file": "/transcripts/sess.jsonl",
                "ingest_mode": "sweep",
            },
        },
    ]

    with (
        patch(
            "agent.training.closet_fidelity.list_closets", return_value=closets
        ),
        patch(
            "agent.training.closet_fidelity.list_drawers", return_value=drawers
        ),
    ):
        result = await run_closet_fidelity(
            tmp_path / "palace", _AlwaysYes(), sample_size=2
        )

    assert result["samples"] == 2
    assert result["diary_samples"] == 1
    assert result["transcript_samples"] == 1
    assert result["diary_fidelity"] == pytest.approx(1.0)
    assert result["transcript_fidelity"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_empty_palace_returns_zero_buckets(tmp_path):
    with patch(
        "agent.training.closet_fidelity.list_closets", return_value=[]
    ):
        result = await run_closet_fidelity(tmp_path / "palace", _AlwaysYes())
    assert result["samples"] == 0
    assert result["diary_fidelity"] == 0.0
    assert result["transcript_fidelity"] == 0.0
