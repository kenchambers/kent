"""Tests for build_snapshot() against a synthetic palace fixture.

FIXED (R16): unit tests — no real server, no port binding, no EventSource
semantics. Runs entirely in-process against tmp_path fixtures.
"""
from pathlib import Path  # noqa: F401 — used by pytest tmp_path fixture type hints


class TestBuildSnapshotEmptyPalace:
    """build_snapshot returns an empty sentinel when no palace exists."""

    def test_missing_sqlite_returns_empty(self, tmp_path: Path) -> None:
        from agent.viz.snapshot import build_snapshot

        result = build_snapshot(tmp_path / "palace", tmp_path)
        assert result == {"nodes": [], "links": [], "empty": True}

    def test_empty_key_present(self, tmp_path: Path) -> None:
        from agent.viz.snapshot import build_snapshot

        result = build_snapshot(tmp_path / "palace", tmp_path)
        assert result.get("empty") is True


class TestBuildSnapshotErrorHandling:
    """FIXED (R7): snapshot must never raise; always returns a dict."""

    def test_corrupt_palace_returns_error_field(self, tmp_path: Path) -> None:
        from agent.viz.snapshot import build_snapshot

        palace = tmp_path / "palace"
        palace.mkdir()
        # Write a non-SQLite file so chroma raises on open.
        (palace / "chroma.sqlite3").write_bytes(b"not a sqlite db")

        result = build_snapshot(palace, tmp_path)
        # Must not raise; must return error field.
        assert "error" in result
        # nodes/links may be partial but must be lists.
        assert isinstance(result.get("nodes"), list)
        assert isinstance(result.get("links"), list)


class TestFirstSeen:
    """Server-side first_seen stamp is set on first snapshot, reused after."""

    def test_first_seen_set(self) -> None:
        import agent.viz.snapshot as snap_mod

        # Reset module-level registry for isolation.
        snap_mod._first_seen.clear()

        node: dict = {"id": "test-node", "val": 1}
        before = __import__("time").time()
        result = snap_mod._stamp_first_seen(node)
        after = __import__("time").time()

        assert before <= result["first_seen"] <= after

    def test_first_seen_reused(self) -> None:
        import agent.viz.snapshot as snap_mod

        snap_mod._first_seen.clear()
        node1 = {"id": "same-id", "val": 1}
        node2 = {"id": "same-id", "val": 2}
        ts1 = snap_mod._stamp_first_seen(node1)["first_seen"]
        ts2 = snap_mod._stamp_first_seen(node2)["first_seen"]
        assert ts1 == ts2
