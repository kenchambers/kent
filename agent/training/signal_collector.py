"""
SignalCollector: observe agent loop events and extract training signals.

Default off. Opt in via KENT_SCOUT_ENABLED=1.

Wired into _run_once in cli.py. Flushes one fsync per turn-cycle (on Terminal).
Signals written to ~/.kent/scout/signals.jsonl.

Supported signal kinds:
  recall_miss         - memory_recall returned "(no memories found)"
  recall_unused       - recall returned content but assistant didn't cite it
  diary_fidelity_gap  - wing recall empty while wing diary has entries
  wing_scope_mismatch - wing recall empty but global recall succeeds
  repeated_topic      - same query seen more than once in session
  tunnel_uncited      - tunnel_create called but not referenced in assistant response
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

KENT_HOME = Path(os.environ.get("KENT_HOME", "~/.kent")).expanduser()
SIGNALS_PATH = KENT_HOME / "scout" / "signals.jsonl"
RECALL_MISS_SENTINEL = "(no memories found)"
MEMORY_RECALL_TOOLS = {"memory_recall", "memory_recall_here"}
# Minimum keyword overlap ratio to count recall as "cited"
_CITE_OVERLAP_MIN = 0.15
# Minimum query similarity to count as repeated topic
_REPEAT_SIM_MIN = 0.6
# Recent queries to keep for repeat detection
_REPEAT_WINDOW = 20


class SignalCollector:
    """
    Observes agent loop events and extracts recall signals.

    Call observe(ev) for each event yielded by agent.loop.run.
    Flush is automatic on Terminal; also call flush() manually if needed.
    """

    def __init__(
        self,
        session_id: str | None = None,
        active_wing: str | None = None,
        path: Path = SIGNALS_PATH,
        memory_store: Any = None,
    ) -> None:
        self.session_id = session_id
        self.active_wing = active_wing
        self.path = path
        self._memory_store = memory_store

        # Per-turn tracking
        self._pending_calls: dict[str, str] = {}     # call_id → tool name
        self._pending_queries: dict[str, str] = {}   # call_id → query string
        self._pending_recall_output: dict[str, str] = {}  # call_id → recall text (non-empty)
        self._pending_tunnel_calls: set[str] = set() # call_ids of tunnel_create not yet verified

        # Session tracking
        self._recent_queries: list[str] = []         # for repeated_topic detection
        self._mid_chat_fired: bool = False            # one mid-chat prompt per session
        self._batch: list[dict] = []

    def update_wing(self, wing: str | None) -> None:
        self.active_wing = wing

    def observe(self, ev: Any) -> None:
        """Dispatch an agent loop event."""
        # Import here to keep module importable without agent package on path
        from agent.events import (
            ToolCallComplete, ToolResult, AssistantMessageComplete, Terminal
        )

        if isinstance(ev, ToolCallComplete):
            self._on_tool_call(ev)
        elif isinstance(ev, ToolResult):
            self._on_tool_result(ev)
        elif isinstance(ev, AssistantMessageComplete):
            self._on_assistant_message(ev)
        elif isinstance(ev, Terminal):
            self.flush()

    # ── event handlers ──────────────────────────────────────────────────────

    def _on_tool_call(self, ev: Any) -> None:
        name = ev.call.name
        cid = ev.call.call_id
        if name in MEMORY_RECALL_TOOLS:
            self._pending_calls[cid] = name
            query = ev.call.arguments.get("query", "")
            self._pending_queries[cid] = query
            # Repeated-topic detection
            if query:
                self._check_repeated_topic(query, cid)
                self._recent_queries.append(query)
                if len(self._recent_queries) > _REPEAT_WINDOW:
                    self._recent_queries.pop(0)
        elif name == "tunnel_create":
            self._pending_tunnel_calls.add(cid)

    def _on_tool_result(self, ev: Any) -> None:
        cid = ev.call_id
        output = ev.output if isinstance(ev.output, str) else json.dumps(ev.output)
        call_name = self._pending_calls.pop(cid, None)
        query = self._pending_queries.pop(cid, None)

        if call_name is None:
            return

        is_miss = RECALL_MISS_SENTINEL in output

        if is_miss:
            self._batch.append(self._make_signal("recall_miss", query=query))

            # wing_scope_mismatch: wing recall empty, try global
            if call_name == "memory_recall_here" and self._memory_store is not None:
                try:
                    global_result = self._memory_store.recall(query or "")
                    if global_result and RECALL_MISS_SENTINEL not in global_result:
                        self._batch.append(self._make_signal("wing_scope_mismatch", query=query))
                except Exception:
                    pass

            # diary_fidelity_gap: wing recall empty but wing diary has entries
            if call_name == "memory_recall_here" and self.active_wing:
                try:
                    self._check_diary_fidelity_gap(query)
                except Exception:
                    pass
        else:
            # recall succeeded — track for recall_unused detection
            self._pending_recall_output[cid] = output

    def _on_assistant_message(self, ev: Any) -> None:
        content = ev.message.content or ""

        # recall_unused: recall returned content but assistant didn't cite it
        for cid, recall_text in list(self._pending_recall_output.items()):
            if recall_text and not self._is_cited(recall_text, content):
                self._batch.append(self._make_signal("recall_unused", query=None))
        self._pending_recall_output.clear()

        # tunnel_uncited: tunnel_create fired but not referenced in response
        if self._pending_tunnel_calls:
            lower = content.lower()
            uncited = {cid for cid in self._pending_tunnel_calls
                       if "tunnel" not in lower and "connected" not in lower}
            if uncited:
                self._batch.append(self._make_signal("tunnel_uncited", query=None))
            self._pending_tunnel_calls.clear()

    # ── helpers ─────────────────────────────────────────────────────────────

    def _check_repeated_topic(self, query: str, cid: str) -> None:
        for prev in self._recent_queries:
            if _token_similarity(prev, query) >= _REPEAT_SIM_MIN:
                self._batch.append(self._make_signal("repeated_topic", query=query))
                break

    def _check_diary_fidelity_gap(self, query: str | None) -> None:
        if not self.active_wing or self._memory_store is None:
            return
        try:
            from agent.memory.mempalace_store import MemPalaceStore
            if not isinstance(self._memory_store, MemPalaceStore):
                return
            wing_dir = self._memory_store.kent_home / "diaries" / self.active_wing
            if not wing_dir.exists():
                return
            has_entries = any(wing_dir.glob("*.md"))
            if has_entries:
                self._batch.append(self._make_signal("diary_fidelity_gap", query=query))
        except Exception:
            pass

    @staticmethod
    def _is_cited(recall_text: str, response: str) -> bool:
        """Return True if response overlaps enough with recall_text to count as a citation."""
        r_tokens = set(_tokenize(recall_text))
        a_tokens = set(_tokenize(response))
        if not r_tokens:
            return True
        overlap = len(r_tokens & a_tokens) / len(r_tokens)
        return overlap >= _CITE_OVERLAP_MIN

    def _make_signal(self, kind: str, *, query: str | None) -> dict:
        ts = time.time()
        id_src = kind + str(ts) + (query or "") + (self.session_id or "")
        sig_id = hashlib.sha256(id_src.encode()).hexdigest()[:16]
        return {
            "id": sig_id,
            "kind": kind,
            "ts": ts,
            "session_id": self.session_id,
            "wing": self.active_wing,
            "query": query,
            "drawer_ids": [],   # reserved; empty until MemoryRecall surfaces IDs
            "metric_value": None,
        }

    def flush(self) -> None:
        if not self._batch:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                for row in self._batch:
                    f.write(json.dumps(row) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        self._batch = []

    @property
    def mid_chat_fired(self) -> bool:
        return self._mid_chat_fired

    def mark_mid_chat_fired(self) -> None:
        self._mid_chat_fired = True


# ─── text utilities ──────────────────────────────────────────────────────── #

_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would could should may might must can i you he she it we they "
    "what when where how why who which this that these those and or but "
    "not no yes in on at to of for with from by about".split()
)


def _tokenize(text: str) -> list[str]:
    import re
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 2]


def _token_similarity(a: str, b: str) -> float:
    ta = set(_tokenize(a))
    tb = set(_tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
