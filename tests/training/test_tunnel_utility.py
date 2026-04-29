"""Tests for tunnel utility logging + summarization (plan §Game D)."""
from __future__ import annotations

import json
from unittest.mock import patch

from agent.training.tunnel_utility import (
    log_tunnel_utility,
    log_rollout_tunnel_observations,
    summarize_tunnel_metrics,
)


def test_log_tunnel_utility_appends_jsonl(tmp_path):
    log_tunnel_utility(
        "rid-1", "tunnel-x", cited=True, query="what did we decide?", wing="main",
        metrics_dir=tmp_path,
    )
    log_tunnel_utility(
        "rid-2", "tunnel-x", cited=False, wing="main", metrics_dir=tmp_path,
    )
    path = tmp_path / "tunnel_utility.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["tunnel_id"] == "tunnel-x"
    assert rec["cited"] is True


def test_log_rollout_no_tunnels_is_quiet(tmp_path):
    with patch("agent.training.tunnel_utility.list_palace_tunnels", return_value=[]):
        n = log_rollout_tunnel_observations(
            "rid", [{"role": "user", "content": "hi"}],
            active_wing="main", metrics_dir=tmp_path,
        )
    assert n == 0
    assert not (tmp_path / "tunnel_utility.jsonl").exists()


def test_log_rollout_marks_cited_when_drawer_id_in_final_answer(tmp_path):
    tunnels = [
        {
            "id": "tun-1",
            "source": {"wing": "main", "room": "daily", "drawer_id": "drawer-aaa"},
            "target": {"wing": "work", "room": "daily", "drawer_id": "drawer-bbb"},
        }
    ]
    transcript = [
        {"role": "user", "content": "anything related?"},
        {"role": "assistant", "content": "calling memory_recall to check"},
        {
            "role": "tool",
            "content": "results: drawer-aaa, snippet about thing",
        },
        {
            "role": "assistant",
            "content": "Found it — see drawer-aaa for context.",
        },
    ]
    with patch(
        "agent.training.tunnel_utility.list_palace_tunnels", return_value=tunnels
    ):
        n = log_rollout_tunnel_observations(
            "rid-cite", transcript, active_wing="main", metrics_dir=tmp_path,
        )
    assert n == 1
    rec = json.loads((tmp_path / "tunnel_utility.jsonl").read_text().strip())
    assert rec["tunnel_id"] == "tun-1"
    assert rec["cited"] is True


def test_log_rollout_uncited_when_recall_appears_but_answer_doesnt_quote(tmp_path):
    tunnels = [
        {
            "id": "tun-2",
            "source": {"wing": "main", "drawer_id": "drawer-zzz"},
            "target": {"wing": "work", "drawer_id": "drawer-yyy"},
        }
    ]
    transcript = [
        {"role": "assistant", "content": "memory_recall(query='thing')"},
        {"role": "tool", "content": "results include drawer-zzz: blah"},
        {"role": "assistant", "content": "I have no good answer."},
    ]
    with patch(
        "agent.training.tunnel_utility.list_palace_tunnels", return_value=tunnels
    ):
        log_rollout_tunnel_observations(
            "rid-uncite", transcript, active_wing="main", metrics_dir=tmp_path,
        )
    rec = json.loads((tmp_path / "tunnel_utility.jsonl").read_text().strip())
    assert rec["cited"] is False


def test_summarize_tunnel_metrics_aggregates(tmp_path):
    log_tunnel_utility("r1", "t-A", cited=True, metrics_dir=tmp_path)
    log_tunnel_utility("r2", "t-A", cited=False, metrics_dir=tmp_path)
    log_tunnel_utility("r3", "t-B", cited=True, metrics_dir=tmp_path)

    summary = summarize_tunnel_metrics(metrics_dir=tmp_path)
    assert summary["observations"] == 3
    assert abs(summary["citation_rate"] - 2 / 3) < 1e-6
    assert summary["by_tunnel"]["t-A"] == {"seen": 2, "cited": 1}
    assert summary["by_tunnel"]["t-B"] == {"seen": 1, "cited": 1}


def test_summarize_tunnel_metrics_empty(tmp_path):
    summary = summarize_tunnel_metrics(metrics_dir=tmp_path)
    assert summary == {"observations": 0, "citation_rate": 0.0, "by_tunnel": {}}
