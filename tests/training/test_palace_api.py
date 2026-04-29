"""Tests for the palace-API helper used by Track 2 games."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.training._palace_api import (
    classify_drawer,
    list_drawers,
    list_closets,
    search_drawers,
)


def test_classify_drawer_diary_prefix():
    assert classify_drawer("drawer_diary_abc123", {}) == "diary"


def test_classify_drawer_sweep_prefix():
    assert classify_drawer("sweep_session_uuid", {}) == "transcript"


def test_classify_drawer_falls_back_to_metadata_ingest_mode():
    assert (
        classify_drawer("custom_id_xyz", {"ingest_mode": "sweep"}) == "transcript"
    )


def test_classify_drawer_diary_via_session_metadata():
    assert (
        classify_drawer("custom_id_xyz", {"source_session": "daily_diary"})
        == "diary"
    )


def test_classify_drawer_unknown():
    assert classify_drawer("opaque_id", {"unrelated": "x"}) == "unknown"


def test_list_drawers_returns_normalized_records(tmp_path):
    fake_col = MagicMock()
    fake_col.get.return_value = {
        "ids": ["drawer_diary_a", "sweep_b"],
        "documents": ["body a", "body b"],
        "metadatas": [
            {"wing": "main", "room": "daily", "source_session": "daily_diary"},
            {"wing": "work", "room": "general", "ingest_mode": "sweep"},
        ],
    }
    with patch(
        "agent.training._palace_api.get_collection", return_value=fake_col, create=True
    ) if False else patch(
        "mempalace.palace.get_collection", return_value=fake_col
    ):
        rows = list_drawers(tmp_path / "palace")

    assert len(rows) == 2
    assert rows[0]["id"] == "drawer_diary_a"
    assert rows[0]["source"] == "diary"
    assert rows[0]["wing"] == "main"
    assert rows[1]["source"] == "transcript"


def test_list_drawers_handles_missing_palace(tmp_path):
    with patch(
        "mempalace.palace.get_collection", side_effect=RuntimeError("no palace")
    ):
        rows = list_drawers(tmp_path / "palace")
    assert rows == []


def test_list_closets_normalizes_results(tmp_path):
    fake_col = MagicMock()
    fake_col.get.return_value = {
        "ids": ["closet_diary_a_01"],
        "documents": ["topic|entities|→drawer"],
        "metadatas": [
            {"wing": "main", "room": "daily", "source_file": "/diaries/main/d.md"}
        ],
    }
    with patch(
        "mempalace.palace.get_closets_collection", return_value=fake_col
    ):
        rows = list_closets(tmp_path / "palace")
    assert rows == [
        {
            "id": "closet_diary_a_01",
            "content": "topic|entities|→drawer",
            "wing": "main",
            "room": "daily",
            "metadata": {
                "wing": "main",
                "room": "daily",
                "source_file": "/diaries/main/d.md",
            },
        }
    ]


def test_search_drawers_unwraps_chroma_query_shape(tmp_path):
    fake_col = MagicMock()
    fake_col.query.return_value = {
        "ids": [["drawer_diary_a", "sweep_b"]],
        "documents": [["body a", "body b"]],
        "metadatas": [[{"wing": "main"}, {"wing": "work"}]],
        "distances": [[0.1, 0.4]],
    }
    with patch("mempalace.palace.get_collection", return_value=fake_col):
        with patch(
            "mempalace.searcher.build_where_filter", return_value=None
        ):
            results = search_drawers(tmp_path / "palace", "color", k=2)

    assert [r["id"] for r in results] == ["drawer_diary_a", "sweep_b"]
    assert results[0]["similarity"] == 0.9
    assert results[1]["wing"] == "work"
