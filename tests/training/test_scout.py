"""Unit tests for agent.training.scout — offline, no LLM calls."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.training.scout import (
    ACCEPTED,
    EXPIRED,
    PENDING,
    REJECTED,
    MID_CHAT_SCORE_THRESHOLD,
    ResourceRecommendation,
    SuggestionStore,
    TrainingPathway,
    _clamp,
    _sha,
    _volume,
    analyze,
    record_pathway,
    record_pathway_for_suggestion,
    read_pathways,
)


# ─── fixtures ────────────────────────────────────────────────────────────────

def _make_rec(
    resource: str = "query_rewrite_policy",
    score: float = 0.5,
    status: str = PENDING,
    day: str = "2026-04-29",
) -> ResourceRecommendation:
    sid = _sha(resource + day)
    return ResourceRecommendation(
        resource=resource,
        score=score,
        rationale="test rationale",
        supporting_signal_ids=["sig1"],
        example_path=None,
        suggested_at=time.time(),
        status=status,
        primary_critic_score=None,
        suggestion_id=sid,
    )


def _write_wake_up(metrics_path: Path, rows: list[dict]) -> None:
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with metrics_path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _write_signals(signals_path: Path, sigs: list[dict]) -> None:
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    with signals_path.open("w") as f:
        for s in sigs:
            f.write(json.dumps(s) + "\n")


# ─── scoring helpers ─────────────────────────────────────────────────────────

def test_volume_breakpoints():
    assert _volume(1) == pytest.approx(0.3)
    assert _volume(5) == pytest.approx(0.7)
    assert _volume(20) == pytest.approx(1.0)
    assert _volume(100) == pytest.approx(1.0)
    assert _volume(0) == pytest.approx(0.0)


def test_clamp():
    assert _clamp(-1.0) == 0.0
    assert _clamp(2.0) == 1.0
    assert _clamp(0.5) == pytest.approx(0.5)


# ─── SuggestionStore ─────────────────────────────────────────────────────────

def test_store_save_and_read(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    rec = _make_rec()
    store.save(rec)
    pending = store.list_pending()
    assert len(pending) == 1
    assert pending[0]["resource"] == "query_rewrite_policy"


def test_store_dedup_is_known(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    rec = _make_rec()
    store.save(rec)
    assert store.is_known(rec.suggestion_id)


def test_store_logical_delete_reject(tmp_path):
    """Appending rejected after pending for same suggestion_id → reads return rejected."""
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    rec = _make_rec()
    store.save(rec)
    assert store.exists_pending(rec.suggestion_id)

    store.update_status(rec.suggestion_id, REJECTED)
    assert not store.exists_pending(rec.suggestion_id)
    assert store.get(rec.suggestion_id)["status"] == REJECTED


def test_store_logical_delete_accept(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    rec = _make_rec()
    store.save(rec)
    store.update_status(rec.suggestion_id, ACCEPTED)
    assert store.get(rec.suggestion_id)["status"] == ACCEPTED


def test_store_multiple_resources(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    rec1 = _make_rec("query_rewrite_policy")
    rec2 = _make_rec("scope_policy", day="2026-04-30")
    store.save(rec1)
    store.save(rec2)
    pending = store.list_pending()
    resources = {r["resource"] for r in pending}
    assert "query_rewrite_policy" in resources
    assert "scope_policy" in resources


def test_store_expire_old(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    old_ts = time.time() - 40 * 86400  # 40 days ago
    sid = _sha("query_rewrite_policy2020-01-01")
    store._append({
        "suggestion_id": sid,
        "resource": "query_rewrite_policy",
        "score": 0.5,
        "rationale": "old",
        "supporting_signal_ids": [],
        "example_path": None,
        "suggested_at": old_ts,
        "status": PENDING,
        "primary_critic_score": None,
    })
    n = store.expire_old()
    assert n == 1
    assert store.get(sid)["status"] == EXPIRED


def test_store_count_pending(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    store.save(_make_rec("query_rewrite_policy", day="2026-04-01"))
    store.save(_make_rec("scope_policy", day="2026-04-02"))
    assert store.count_pending() == 2


def test_store_list_pending_sorted_by_score(tmp_path):
    store = SuggestionStore(tmp_path / "suggestions.jsonl")
    store.save(_make_rec("query_rewrite_policy", score=0.3, day="2026-04-01"))
    store.save(_make_rec("scope_policy", score=0.8, day="2026-04-02"))
    pending = store.list_pending()
    assert pending[0]["score"] == pytest.approx(0.8)


# ─── analyze() ───────────────────────────────────────────────────────────────

def test_analyze_recall_miss_signal(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    sig = {
        "id": "abc123",
        "kind": "recall_miss",
        "ts": time.time(),
        "session_id": None,
        "wing": None,
        "query": "test query",
        "drawer_ids": [],
        "metric_value": None,
    }
    _write_signals(signals_path, [sig])

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "query_rewrite_policy" for r in recs)


def test_analyze_low_recall_a1(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    rows = [{"ts": time.time(), "round": i, "recall_a1": 0.1} for i in range(5)]
    _write_wake_up(metrics_dir / "wake_up.jsonl", rows)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "query_rewrite_policy" for r in recs)


def test_analyze_low_scope_accuracy(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    rows = [{"ts": time.time(), "round": i, "scope_accuracy": 0.2} for i in range(5)]
    _write_wake_up(metrics_dir / "wake_up.jsonl", rows)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "scope_policy" for r in recs)


def test_analyze_wing_scope_mismatch_signal(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    sigs = [{"id": f"ws{i}", "kind": "wing_scope_mismatch", "ts": time.time(),
             "session_id": None, "wing": "research", "query": "q",
             "drawer_ids": [], "metric_value": None} for i in range(3)]
    _write_signals(signals_path, sigs)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "scope_policy" for r in recs)


def test_analyze_recall_unused_signal(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    sigs = [{"id": f"ru{i}", "kind": "recall_unused", "ts": time.time(),
             "session_id": None, "wing": None, "query": None,
             "drawer_ids": [], "metric_value": None} for i in range(3)]
    _write_signals(signals_path, sigs)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "retrieval_policy" for r in recs)


def test_analyze_tunnel_uncited_signal(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    sigs = [{"id": f"tu{i}", "kind": "tunnel_uncited", "ts": time.time(),
             "session_id": None, "wing": None, "query": None,
             "drawer_ids": [], "metric_value": None} for i in range(2)]
    _write_signals(signals_path, sigs)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "retrieval_policy" for r in recs)


def test_analyze_repeated_topic_signal(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    sigs = [{"id": f"rt{i}", "kind": "repeated_topic", "ts": time.time(),
             "session_id": None, "wing": None, "query": "same topic",
             "drawer_ids": [], "metric_value": None} for i in range(2)]
    _write_signals(signals_path, sigs)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert any(r.resource == "query_rewrite_policy" for r in recs)


def test_analyze_no_signal_no_recommendation(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    rows = [{"ts": time.time(), "round": i, "recall_a1": 0.8, "scope_accuracy": 0.9}
            for i in range(5)]
    _write_wake_up(metrics_dir / "wake_up.jsonl", rows)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert recs == []


def test_analyze_dedup_same_day(tmp_path):
    """Running analyze twice with same signal on same day → 1 row total."""
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    sig = {"id": "abc", "kind": "recall_miss", "ts": time.time(),
           "session_id": None, "wing": None, "query": "q",
           "drawer_ids": [], "metric_value": None}
    _write_signals(signals_path, [sig])

    recs1 = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                    metrics_dir=metrics_dir)
    recs2 = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                    metrics_dir=metrics_dir)
    assert len(recs1) >= 1
    assert len(recs2) == 0  # deduped


def test_analyze_drift_suppressed(tmp_path):
    """Resource with recent drift → no recommendation."""
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    # Write a recent drift event for query_rewrite_policy
    drift_path = metrics_dir / "drift.jsonl"
    drift_path.parent.mkdir(parents=True, exist_ok=True)
    drift_path.write_text(json.dumps({
        "resource": "query_rewrite_policy",
        "drift_detected": True,
        "ts": time.time(),
    }) + "\n")

    sig = {"id": "x1", "kind": "recall_miss", "ts": time.time(),
           "session_id": None, "wing": None, "query": "q",
           "drawer_ids": [], "metric_value": None}
    _write_signals(signals_path, [sig])

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert all(r.resource != "query_rewrite_policy" for r in recs)


def test_analyze_sorted_by_score_descending(tmp_path):
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"

    # Both scope_accuracy_low and recall_miss signals
    sigs = [{"id": f"rm{i}", "kind": "recall_miss", "ts": time.time(),
             "session_id": None, "wing": None, "query": "q",
             "drawer_ids": [], "metric_value": None} for i in range(20)]
    _write_signals(signals_path, sigs)

    rows = [{"ts": time.time(), "round": i, "scope_accuracy": 0.1} for i in range(5)]
    _write_wake_up(metrics_dir / "wake_up.jsonl", rows)

    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    scores = [r.score for r in recs]
    assert scores == sorted(scores, reverse=True)


# ─── record_pathway ───────────────────────────────────────────────────────────

def test_record_pathway(tmp_path):
    pathway = TrainingPathway(
        pathway_id="pw001",
        created_at=time.time(),
        suggestion_id="sg001",
        resource="query_rewrite_policy",
        signal_ids=["s1", "s2"],
        drawer_ids=[],
        wing="research",
        queries=["what did I read?"],
        train_run_id="run001",
        pre_train_score=0.5,
        post_train_score=0.7,
        drift_detected=False,
        examples_dir=str(tmp_path / "examples"),
    )
    path = tmp_path / "pathways.jsonl"
    record_pathway(pathway, path=path)
    assert path.exists()
    row = json.loads(path.read_text().strip())
    assert row["resource"] == "query_rewrite_policy"
    assert row["wing"] == "research"
    assert row["drift_detected"] is False
    assert row["signal_ids"] == ["s1", "s2"]


def test_read_pathways_empty(tmp_path):
    rows = read_pathways(tmp_path / "nonexistent.jsonl")
    assert rows == []


def test_record_pathway_for_suggestion_with_drift(tmp_path):
    """Wires accepted suggestion + drift.jsonl entry → populated pathway row."""
    suggestions_path = tmp_path / "suggestions.jsonl"
    pathways_path = tmp_path / "pathways.jsonl"
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    store = SuggestionStore(suggestions_path)
    rec = _make_rec()
    store.save(rec)
    store.update_status(rec.suggestion_id, ACCEPTED)

    drift_path = metrics_dir / "drift.jsonl"
    drift_path.write_text(json.dumps({
        "ts": time.time(),
        "resource": "query_rewrite_policy",
        "pair": "gpt-4o-mini+gpt-4o",
        "val_reward": 0.82,
        "holdout_reward": 0.75,
        "gap": 0.07,
        "drift_detected": False,
        "holdout_n": 10,
    }) + "\n")

    pathway = record_pathway_for_suggestion(
        rec.suggestion_id, store=store,
        metrics_dir=metrics_dir, pathways_path=pathways_path,
    )
    assert pathway is not None
    assert pathway.resource == "query_rewrite_policy"
    assert pathway.suggestion_id == rec.suggestion_id
    assert pathway.drift_detected is False
    assert pathway.post_train_score == pytest.approx(0.82)
    assert pathways_path.exists()
    rows = read_pathways(pathways_path)
    assert len(rows) == 1
    assert rows[0]["suggestion_id"] == rec.suggestion_id


def test_record_pathway_for_suggestion_no_drift_file(tmp_path):
    """No drift.jsonl → pathway still recorded with None scores."""
    suggestions_path = tmp_path / "suggestions.jsonl"
    pathways_path = tmp_path / "pathways.jsonl"
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    store = SuggestionStore(suggestions_path)
    rec = _make_rec()
    store.save(rec)

    pathway = record_pathway_for_suggestion(
        rec.suggestion_id, store=store,
        metrics_dir=metrics_dir, pathways_path=pathways_path,
    )
    assert pathway is not None
    assert pathway.drift_detected is None
    assert pathway.post_train_score is None


def test_record_pathway_for_suggestion_unknown(tmp_path):
    """Unknown suggestion_id → returns None, no row written."""
    suggestions_path = tmp_path / "suggestions.jsonl"
    pathways_path = tmp_path / "pathways.jsonl"
    store = SuggestionStore(suggestions_path)
    result = record_pathway_for_suggestion(
        "deadbeef", store=store,
        metrics_dir=tmp_path / "metrics", pathways_path=pathways_path,
    )
    assert result is None
    assert not pathways_path.exists()


def test_read_pathways_multiple(tmp_path):
    path = tmp_path / "pathways.jsonl"
    for i in range(3):
        pathway = TrainingPathway(
            pathway_id=f"pw{i}",
            created_at=time.time() + i,
            suggestion_id=f"sg{i}",
            resource="scope_policy",
            signal_ids=[],
            drawer_ids=[],
            wing=None,
            queries=[],
            train_run_id=f"run{i}",
            pre_train_score=None,
            post_train_score=None,
            drift_detected=None,
            examples_dir="",
        )
        record_pathway(pathway, path=path)
    rows = read_pathways(path)
    assert len(rows) == 3


# ─── reward-hacking probe ────────────────────────────────────────────────────

def test_no_signal_no_recommendation_injection_resistant(tmp_path):
    """Confident-wrong assistant turns with no memory_recall → zero recommendations."""
    signals_path = tmp_path / "scout" / "signals.jsonl"
    suggestions_path = tmp_path / "scout" / "suggestions.jsonl"
    metrics_dir = tmp_path / "metrics"
    # Signals file deliberately absent — no observable failures
    recs = analyze(signals_path=signals_path, suggestions_path=suggestions_path,
                   metrics_dir=metrics_dir)
    assert recs == []
