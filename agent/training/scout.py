"""
Scout: behavioral signal aggregation → training suggestions.

Reads signals from ~/.kent/scout/signals.jsonl and metrics from
~/.kent/lightning_store/metrics/ to produce ranked ResourceRecommendation rows.

Cron-safe (zero LLM calls). mine_examples() is REPL-only (requires LLM).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

KENT_HOME = Path(os.environ.get("KENT_HOME", "~/.kent")).expanduser()
SCOUT_DIR = KENT_HOME / "scout"
SIGNALS_PATH = SCOUT_DIR / "signals.jsonl"
SUGGESTIONS_PATH = SCOUT_DIR / "suggestions.jsonl"
PATHWAYS_PATH = SCOUT_DIR / "pathways.jsonl"
METRICS_DIR = KENT_HOME / "lightning_store" / "metrics"

RECALL_MISS_SENTINEL = "(no memories found)"
RECALL_A1_THRESHOLD = 0.4
SCOPE_ACCURACY_THRESHOLD = 0.5
DRIFT_SUPPRESSION_DAYS = 30
SUGGESTION_EXPIRY_DAYS = 30
ROLLING_WINDOW = 5  # last N wake_up rounds for metric signals
MID_CHAT_SCORE_THRESHOLD = 0.7

# volume: signal count → volume score
_VOLUME_TABLE = [(1, 0.3), (5, 0.7), (20, 1.0)]

# Signal kinds and their target resources
SIGNAL_RESOURCES: dict[str, str] = {
    "recall_miss": "query_rewrite_policy",
    "recall_a1_low": "query_rewrite_policy",
    "recall_unused": "retrieval_policy",
    "diary_fidelity_gap": "query_rewrite_policy",
    "repeated_topic": "query_rewrite_policy",
    "scope_accuracy_low": "scope_policy",
    "wing_scope_mismatch": "scope_policy",
    "tunnel_uncited": "retrieval_policy",
}

# Valid suggestion statuses
PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
EXPIRED = "expired"
ACCEPTED_DRIFTED = "accepted_drifted"


@dataclass
class Signal:
    id: str
    kind: str
    ts: float
    session_id: str | None
    wing: str | None
    query: str | None
    drawer_ids: list[str]          # reserved; empty until MemoryRecall surfaces IDs
    metric_value: float | None


@dataclass
class ResourceRecommendation:
    resource: str
    score: float                   # severity × volume, 0..1
    rationale: str
    supporting_signal_ids: list[str]
    example_path: Path | None
    suggested_at: float
    status: str                    # pending | accepted | rejected | expired | accepted_drifted
    primary_critic_score: float | None
    suggestion_id: str             # sha256(resource + day)[:16]


@dataclass
class TrainingPathway:
    pathway_id: str
    created_at: float
    suggestion_id: str
    resource: str
    signal_ids: list[str]
    drawer_ids: list[str]          # union of signal.drawer_ids; empty until palace API exposes them
    wing: str | None               # modal wing from signals
    queries: list[str]
    train_run_id: str
    pre_train_score: float | None
    post_train_score: float | None
    drift_detected: bool | None
    examples_dir: str              # Path serialized as str


# ─── helpers ──────────────────────────────────────────────────────────────── #

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _volume(count: int) -> float:
    if count <= 0:
        return 0.0
    for threshold, v in reversed(_VOLUME_TABLE):
        if count >= threshold:
            return v
    return _VOLUME_TABLE[0][1]


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


# ─── SuggestionStore ─────────────────────────────────────────────────────── #

class SuggestionStore:
    """
    Append-only JSONL store for ResourceRecommendation rows.

    Status updates use logical-delete: to change status, append a new row with
    the same suggestion_id. Reads collapse by suggestion_id (last-write-wins).
    Locking via fcntl.flock (mirrors agent/memory/diary.py:35-42).
    """

    def __init__(self, path: Path = SUGGESTIONS_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> dict[str, dict]:
        """Collapse rows by suggestion_id with last-write-wins on each field
        (status updates only carry status+ts, so we must merge to preserve
        resource/score/rationale from the original save)."""
        if not self.path.exists():
            return {}
        rows: dict[str, dict] = {}
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    sid = row["suggestion_id"]
                    if sid in rows:
                        rows[sid].update(row)
                    else:
                        rows[sid] = row
                except (json.JSONDecodeError, KeyError):
                    continue
        return rows

    def _append(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(row) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def is_known(self, suggestion_id: str) -> bool:
        return suggestion_id in self._read_all()

    def exists_pending(self, suggestion_id: str) -> bool:
        rows = self._read_all()
        return suggestion_id in rows and rows[suggestion_id].get("status") == PENDING

    def save(self, rec: ResourceRecommendation) -> None:
        row = {
            "suggestion_id": rec.suggestion_id,
            "resource": rec.resource,
            "score": rec.score,
            "rationale": rec.rationale,
            "supporting_signal_ids": rec.supporting_signal_ids,
            "example_path": str(rec.example_path) if rec.example_path else None,
            "suggested_at": rec.suggested_at,
            "status": rec.status,
            "primary_critic_score": rec.primary_critic_score,
        }
        self._append(row)

    def update_status(self, suggestion_id: str, status: str, ts: float | None = None) -> None:
        self._append({"suggestion_id": suggestion_id, "status": status, "ts": ts or time.time()})

    def expire_old(self) -> int:
        """Mark pending suggestions older than SUGGESTION_EXPIRY_DAYS as expired."""
        rows = self._read_all()
        cutoff = time.time() - SUGGESTION_EXPIRY_DAYS * 86400
        n = 0
        for sid, row in rows.items():
            if row.get("status") == PENDING and row.get("suggested_at", time.time()) < cutoff:
                self.update_status(sid, EXPIRED)
                n += 1
        return n

    def list_pending(self) -> list[dict]:
        rows = self._read_all()
        return sorted(
            [r for r in rows.values() if r.get("status") == PENDING],
            key=lambda r: -r.get("score", 0),
        )

    def get(self, suggestion_id: str) -> dict | None:
        return self._read_all().get(suggestion_id)

    def count_pending(self) -> int:
        return len(self.list_pending())


# ─── Pathway log ─────────────────────────────────────────────────────────── #

def record_pathway(pathway: TrainingPathway, path: Path = PATHWAYS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(pathway)
    row["examples_dir"] = str(pathway.examples_dir)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(row) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_pathways(path: Path = PATHWAYS_PATH) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def record_pathway_for_suggestion(
    suggestion_id: str,
    store: SuggestionStore | None = None,
    metrics_dir: Path = METRICS_DIR,
    pathways_path: Path = PATHWAYS_PATH,
) -> TrainingPathway | None:
    """
    Append a TrainingPathway row linking an accepted suggestion to its train run.

    Reads the most recent drift.jsonl entry for the suggestion's resource to
    populate drift_detected and post_train_score. Score fields and drawer_ids
    are best-effort: see plan §risk #8 (val_reward truthiness bug) and §risk #6
    (palace API gap). Returns the recorded pathway, or None if suggestion missing.
    """
    if store is None:
        store = SuggestionStore()
    row = store.get(suggestion_id)
    if not row:
        return None
    resource = row.get("resource", "")

    pre_score: float | None = None
    post_score: float | None = None
    drift_detected: bool | None = None
    train_run_id = ""
    drift_path = metrics_dir / "drift.jsonl"
    if drift_path.exists():
        latest = None
        with open(drift_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("resource") == resource:
                        latest = r
                except json.JSONDecodeError:
                    continue
        if latest:
            post_score = latest.get("val_reward")
            drift_detected = latest.get("drift_detected")
            train_run_id = f"{latest.get('pair', '')}@{latest.get('ts', 0)}"

    pathway = TrainingPathway(
        pathway_id=_sha(suggestion_id + str(time.time())),
        created_at=time.time(),
        suggestion_id=suggestion_id,
        resource=resource,
        signal_ids=row.get("supporting_signal_ids", []) or [],
        drawer_ids=[],
        wing=None,
        queries=[],
        train_run_id=train_run_id,
        pre_train_score=pre_score,
        post_train_score=post_score,
        drift_detected=drift_detected,
        examples_dir="",
    )
    record_pathway(pathway, path=pathways_path)
    return pathway


# ─── Signal reading ──────────────────────────────────────────────────────── #

def _read_signals(path: Path = SIGNALS_PATH, window_days: int = 7) -> list[dict]:
    if not path.exists():
        return []
    cutoff = time.time() - window_days * 86400
    signals = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                if s.get("ts", 0) >= cutoff:
                    signals.append(s)
            except json.JSONDecodeError:
                continue
    return signals


def _read_wake_up_metrics(path: Path, window: int = ROLLING_WINDOW) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-window:]


def _is_drift_suppressed(resource: str, metrics_dir: Path) -> bool:
    drift_path = metrics_dir / "drift.jsonl"
    if not drift_path.exists():
        return False
    cutoff = time.time() - DRIFT_SUPPRESSION_DAYS * 86400
    with open(drift_path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line.strip())
                if (row.get("resource") == resource
                        and row.get("drift_detected")
                        and row.get("ts", 0) >= cutoff):
                    return True
            except (json.JSONDecodeError, KeyError):
                continue
    return False


# ─── Core analysis ───────────────────────────────────────────────────────── #

def analyze(
    window_days: int = 7,
    metrics_dir: Path = METRICS_DIR,
    signals_path: Path = SIGNALS_PATH,
    suggestions_path: Path = SUGGESTIONS_PATH,
) -> list[ResourceRecommendation]:
    """
    Aggregate signals and metrics; return newly-created ResourceRecommendation list.

    Zero LLM calls — safe to run from cron (kent scout).
    """
    from .apo_runner import RESOURCE_ORDER

    store = SuggestionStore(suggestions_path)
    store.expire_old()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    all_signals = _read_signals(signals_path, window_days=window_days)
    wake_rows = _read_wake_up_metrics(metrics_dir / "wake_up.jsonl")
    recommendations: list[ResourceRecommendation] = []

    # Group signals by kind for reuse
    by_kind: dict[str, list[dict]] = {}
    for s in all_signals:
        by_kind.setdefault(s.get("kind", ""), []).append(s)

    def _maybe_add(resource: str, severity: float, volume_count: int,
                   rationale: str, signal_ids: list[str]) -> None:
        if resource not in RESOURCE_ORDER:
            return
        if _is_drift_suppressed(resource, metrics_dir):
            return
        score = _clamp(severity * _volume(volume_count))
        if score <= 0:
            return
        sid = _sha(resource + today)
        if store.is_known(sid):
            return
        rec = ResourceRecommendation(
            resource=resource,
            score=score,
            rationale=rationale,
            supporting_signal_ids=signal_ids,
            example_path=None,
            suggested_at=time.time(),
            status=PENDING,
            primary_critic_score=None,
            suggestion_id=sid,
        )
        store.save(rec)
        recommendations.append(rec)

    # ── query_rewrite_policy signals ────────────────────────────────────────

    # Signal: recall_miss (live)
    miss_sigs = by_kind.get("recall_miss", [])
    if miss_sigs:
        _maybe_add(
            "query_rewrite_policy",
            severity=1.0,
            volume_count=len(miss_sigs),
            rationale=f"{len(miss_sigs)} recall_miss event(s) in {window_days}d window",
            signal_ids=[s["id"] for s in miss_sigs if "id" in s],
        )

    # Signal: recall_a1_low (wake_up metrics)
    a1_values = [r["recall_a1"] for r in wake_rows if "recall_a1" in r]
    if a1_values:
        mean_a1 = sum(a1_values) / len(a1_values)
        if mean_a1 < RECALL_A1_THRESHOLD:
            sev = (RECALL_A1_THRESHOLD - mean_a1) / RECALL_A1_THRESHOLD
            _maybe_add(
                "query_rewrite_policy",
                severity=sev,
                volume_count=len(a1_values),
                rationale=f"recall_a1 mean={mean_a1:.3f} < threshold={RECALL_A1_THRESHOLD}",
                signal_ids=[s["id"] for s in by_kind.get("recall_a1_low", []) if "id" in s],
            )

    # Signal: diary_fidelity_gap (live)
    gap_sigs = by_kind.get("diary_fidelity_gap", [])
    if gap_sigs:
        _maybe_add(
            "query_rewrite_policy",
            severity=0.8,
            volume_count=len(gap_sigs),
            rationale=f"{len(gap_sigs)} diary_fidelity_gap event(s): wing diary present but recall returns empty",
            signal_ids=[s["id"] for s in gap_sigs if "id" in s],
        )

    # Signal: repeated_topic (live)
    repeat_sigs = by_kind.get("repeated_topic", [])
    if repeat_sigs:
        _maybe_add(
            "query_rewrite_policy",
            severity=0.6,
            volume_count=len(repeat_sigs),
            rationale=f"{len(repeat_sigs)} repeated_topic event(s): same queries reappearing without being remembered",
            signal_ids=[s["id"] for s in repeat_sigs if "id" in s],
        )

    # ── scope_policy signals ─────────────────────────────────────────────────

    # Signal: scope_accuracy_low (wake_up metrics)
    scope_values = [r["scope_accuracy"] for r in wake_rows if "scope_accuracy" in r]
    if scope_values:
        mean_scope = sum(scope_values) / len(scope_values)
        if mean_scope < SCOPE_ACCURACY_THRESHOLD:
            sev = (SCOPE_ACCURACY_THRESHOLD - mean_scope) / SCOPE_ACCURACY_THRESHOLD
            _maybe_add(
                "scope_policy",
                severity=sev,
                volume_count=len(scope_values),
                rationale=f"scope_accuracy mean={mean_scope:.3f} < threshold={SCOPE_ACCURACY_THRESHOLD}",
                signal_ids=[],
            )

    # Signal: wing_scope_mismatch (live)
    mismatch_sigs = by_kind.get("wing_scope_mismatch", [])
    if mismatch_sigs:
        _maybe_add(
            "scope_policy",
            severity=0.7,
            volume_count=len(mismatch_sigs),
            rationale=f"{len(mismatch_sigs)} wing_scope_mismatch event(s): wing recall empty while global recall succeeds",
            signal_ids=[s["id"] for s in mismatch_sigs if "id" in s],
        )

    # ── retrieval_policy signals ─────────────────────────────────────────────

    # Signal: recall_unused (live)
    unused_sigs = by_kind.get("recall_unused", [])
    if unused_sigs:
        _maybe_add(
            "retrieval_policy",
            severity=0.5,
            volume_count=len(unused_sigs),
            rationale=f"{len(unused_sigs)} recall_unused event(s): memory recalled but not cited in response",
            signal_ids=[s["id"] for s in unused_sigs if "id" in s],
        )

    # Signal: tunnel_uncited (live)
    tunnel_sigs = by_kind.get("tunnel_uncited", [])
    if tunnel_sigs:
        _maybe_add(
            "retrieval_policy",
            severity=0.5,
            volume_count=len(tunnel_sigs),
            rationale=f"{len(tunnel_sigs)} tunnel_uncited event(s): tunnel created but not referenced in response",
            signal_ids=[s["id"] for s in tunnel_sigs if "id" in s],
        )

    recommendations.sort(key=lambda r: r.score, reverse=True)
    return recommendations


# ─── Example mining (REPL-only, not cron) ────────────────────────────────── #

def mine_examples(suggestion_id: str, store: SuggestionStore | None = None) -> Path | None:
    """
    Mine training examples for a pending suggestion.

    NOT called from cron — requires an LLM paraphrase pass.
    v1: returns None (placeholder). Real mining requires mempalace API extension
    to surface which paraphrase validates the same drawer as the failing query.
    """
    if store is None:
        store = SuggestionStore()
    row = store.get(suggestion_id)
    if not row:
        return None
    # v1: real LLM-paraphrase mining is a follow-up
    return None
