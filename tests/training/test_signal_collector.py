"""Unit tests for agent.training.signal_collector — offline, no LLM calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.training.signal_collector import SignalCollector, _token_similarity
from agent.events import ToolCall, ToolCallComplete, ToolResult, Terminal, AssistantMessageComplete, AssistantMessage


def _tc(call_id: str, name: str, **kwargs) -> ToolCallComplete:
    return ToolCallComplete(call=ToolCall(call_id=call_id, name=name, arguments=kwargs))


def _tr(call_id: str, output: str) -> ToolResult:
    return ToolResult(call_id=call_id, output=output)


def _am(content: str) -> AssistantMessageComplete:
    return AssistantMessageComplete(message=AssistantMessage(content=content, tool_calls=[]))


# ─── recall_miss ─────────────────────────────────────────────────────────────

def test_recall_miss_extracted(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(session_id="sess1", active_wing="research", path=path)

    collector.observe(_tc("id1", "memory_recall", query="what did I eat?"))
    collector.observe(_tr("id1", "(no memories found)"))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert any(r["kind"] == "recall_miss" for r in rows)
    recall_rows = [r for r in rows if r["kind"] == "recall_miss"]
    assert recall_rows[0]["query"] == "what did I eat?"
    assert recall_rows[0]["wing"] == "research"


def test_successful_recall_not_a_miss(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("id2", "memory_recall", query="books I read"))
    collector.observe(_tr("id2", "You read 'The Power of Habit' last week."))
    collector.flush()

    # No recall_miss signal
    if path.exists() and path.read_text().strip():
        rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
        assert not any(r["kind"] == "recall_miss" for r in rows)
    else:
        assert True  # nothing written is fine too


def test_non_memory_tool_ignored(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("id3", "web_search", query="python docs"))
    collector.observe(_tr("id3", "(no memories found)"))
    collector.flush()

    if path.exists() and path.read_text().strip():
        rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
        assert not any(r["kind"] == "recall_miss" for r in rows)


def test_memory_recall_here_tracked(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(session_id="sess2", active_wing="cooking", path=path)

    collector.observe(_tc("id4", "memory_recall_here", query="soup recipe"))
    collector.observe(_tr("id4", "(no memories found)"))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert any(r["kind"] == "recall_miss" for r in rows)


def test_terminal_triggers_flush(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("id5", "memory_recall", query="missing memory"))
    collector.observe(_tr("id5", "(no memories found)"))
    collector.observe(Terminal(reason="completed"))

    # flush called by Terminal — file should exist
    assert path.exists()
    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert any(r["kind"] == "recall_miss" for r in rows)


def test_update_wing(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(active_wing="alpha", path=path)
    collector.update_wing("beta")

    collector.observe(_tc("id6", "memory_recall", query="q"))
    collector.observe(_tr("id6", "(no memories found)"))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert rows[0]["wing"] == "beta"


# ─── recall_unused ───────────────────────────────────────────────────────────

def test_recall_unused_when_not_cited(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("id7", "memory_recall", query="favorite food"))
    collector.observe(_tr("id7", "You mentioned loving Thai food and dumplings in March."))
    # Assistant completely ignores the recall
    collector.observe(_am("The weather today is sunny."))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert any(r["kind"] == "recall_unused" for r in rows)


def test_recall_not_unused_when_cited(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    recall_text = "You mentioned loving Thai food and dumplings in March."
    collector.observe(_tc("id8", "memory_recall", query="favorite food"))
    collector.observe(_tr("id8", recall_text))
    # Assistant cites content from recall
    collector.observe(_am("Based on your memories, you love Thai food and dumplings!"))
    collector.flush()

    rows = []
    if path.exists() and path.read_text().strip():
        rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert not any(r["kind"] == "recall_unused" for r in rows)


# ─── tunnel_uncited ───────────────────────────────────────────────────────────

def test_tunnel_uncited(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("t1", "tunnel_create", source="wing_a", target="wing_b"))
    # Assistant doesn't mention tunnel or connected
    collector.observe(_am("Let me look at that code."))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert any(r["kind"] == "tunnel_uncited" for r in rows)


def test_tunnel_cited(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("t2", "tunnel_create", source="wing_a", target="wing_b"))
    # Assistant mentions tunnel
    collector.observe(_am("I've connected the wings via a tunnel between wing_a and wing_b."))
    collector.flush()

    rows = []
    if path.exists() and path.read_text().strip():
        rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert not any(r["kind"] == "tunnel_uncited" for r in rows)


# ─── repeated_topic ───────────────────────────────────────────────────────────

def test_repeated_topic_detected(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    query = "what books have I read recently"
    # First occurrence
    collector.observe(_tc("id9", "memory_recall", query=query))
    collector.observe(_tr("id9", "(no memories found)"))

    # Same query again
    collector.observe(_tc("id10", "memory_recall", query=query))
    collector.observe(_tr("id10", "(no memories found)"))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert any(r["kind"] == "repeated_topic" for r in rows)


def test_different_topics_not_repeated(tmp_path):
    path = tmp_path / "signals.jsonl"
    collector = SignalCollector(path=path)

    collector.observe(_tc("id11", "memory_recall", query="favorite coffee"))
    collector.observe(_tr("id11", "(no memories found)"))

    collector.observe(_tc("id12", "memory_recall", query="programming languages learned"))
    collector.observe(_tr("id12", "(no memories found)"))
    collector.flush()

    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert not any(r["kind"] == "repeated_topic" for r in rows)


# ─── mid_chat_fired ───────────────────────────────────────────────────────────

def test_mid_chat_fired_tracking():
    collector = SignalCollector()
    assert not collector.mid_chat_fired
    collector.mark_mid_chat_fired()
    assert collector.mid_chat_fired


# ─── token_similarity ────────────────────────────────────────────────────────

def test_token_similarity_identical():
    assert _token_similarity("books I read", "books I read") == pytest.approx(1.0)


def test_token_similarity_empty():
    assert _token_similarity("", "anything") == pytest.approx(0.0)


def test_token_similarity_disjoint():
    sim = _token_similarity("apple orange banana", "python javascript rust")
    assert sim == pytest.approx(0.0)


def test_token_similarity_partial():
    sim = _token_similarity("books read recently", "recently books shelf")
    assert sim > 0.0
