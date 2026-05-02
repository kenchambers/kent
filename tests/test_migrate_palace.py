"""Phase C: migration script unit tests."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


def _make_transcript(path: Path, session_id: str, date: str, n_messages: int = 2) -> None:
    """Write a minimal JSONL transcript file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(n_messages):
            role = "user" if i % 2 == 0 else "assistant"
            record = {
                "type": role,
                "sessionId": session_id,
                "uuid": uuid.uuid4().hex,
                "timestamp": f"{date}T12:00:0{i}Z",
                "message": {"role": role, "content": f"message {i}"},
            }
            f.write(json.dumps(record) + "\n")


def _sweep_transcript(transcript_path: Path, palace_path: Path) -> None:
    """Sweep a transcript into the palace so drawers exist to tag."""
    from mempalace.sweeper import sweep
    sweep(str(transcript_path), str(palace_path), source_label="kent")


@pytest.mark.memory
def test_migration_infers_wing_from_diary(tmp_path):
    """Session on 2026-04-15 with a projA diary → tagged wing='projA'."""
    try:
        from mempalace.palace import get_collection
    except ImportError:
        pytest.skip("mempalace not installed")

    kent_home = tmp_path / "kent"
    kent_home.mkdir()
    (kent_home / "active_wing.txt").write_text("default\n")

    palace_path = kent_home / "palace"
    transcript_base = tmp_path / "transcripts"

    # Session 1: has a matching diary on 2026-04-15
    sid1 = uuid.uuid4().hex
    t1 = transcript_base / f"{sid1}.jsonl"
    _make_transcript(t1, sid1, "2026-04-15")
    _sweep_transcript(t1, palace_path)

    # Session 2: no matching diary (different date)
    sid2 = uuid.uuid4().hex
    t2 = transcript_base / f"{sid2}.jsonl"
    _make_transcript(t2, sid2, "2026-03-01")
    _sweep_transcript(t2, palace_path)

    # Create diary for projA on 2026-04-15
    diary_dir = kent_home / "diaries" / "projA"
    diary_dir.mkdir(parents=True)
    (diary_dir / "2026-04-15.md").write_text("# diary\n")

    from agent.migrate import run_migration

    stats = run_migration(
        kent_home=kent_home,
        palace_path=palace_path,
        transcript_base=transcript_base,
        default_wing="kent_default",
        dry_run=False,
    )

    assert stats["sessions_scanned"] >= 2
    assert stats["drawers_to_update"] > 0

    col = get_collection(str(palace_path), create=False)
    all_data = col.get(include=["metadatas"])
    metas = all_data.get("metadatas") or []

    projA_drawers = [m for m in metas if (m or {}).get("wing") == "projA"]
    default_drawers = [m for m in metas if (m or {}).get("wing") == "kent_default"]

    assert projA_drawers, "session 1 drawers should be tagged projA"
    assert default_drawers, "session 2 drawers should be tagged kent_default"

    for m in projA_drawers + default_drawers:
        assert m.get("room") == "conversation"
        assert m.get("migrated_at")


@pytest.mark.memory
def test_migration_idempotent(tmp_path):
    """Re-running migration does not double-tag already-tagged drawers."""
    try:
        from mempalace.palace import get_collection as _gc  # noqa: F401
    except ImportError:
        pytest.skip("mempalace not installed")

    kent_home = tmp_path / "kent"
    kent_home.mkdir()
    (kent_home / "active_wing.txt").write_text("default\n")

    palace_path = kent_home / "palace"
    transcript_base = tmp_path / "transcripts"

    sid = uuid.uuid4().hex
    t = transcript_base / f"{sid}.jsonl"
    _make_transcript(t, sid, "2026-01-01")
    _sweep_transcript(t, palace_path)

    from agent.migrate import run_migration

    stats1 = run_migration(
        kent_home=kent_home, palace_path=palace_path,
        transcript_base=transcript_base, default_wing="kent_default",
    )
    stats2 = run_migration(
        kent_home=kent_home, palace_path=palace_path,
        transcript_base=transcript_base, default_wing="kent_default",
    )

    # Second run should update 0 drawers (all already tagged)
    assert stats2["drawers_to_update"] == 0


@pytest.mark.memory
def test_migration_dry_run_writes_nothing(tmp_path):
    """Dry-run reports counts but leaves drawers untagged."""
    try:
        from mempalace.palace import get_collection
    except ImportError:
        pytest.skip("mempalace not installed")

    kent_home = tmp_path / "kent"
    kent_home.mkdir()
    (kent_home / "active_wing.txt").write_text("default\n")

    palace_path = kent_home / "palace"
    transcript_base = tmp_path / "transcripts"

    sid = uuid.uuid4().hex
    t = transcript_base / f"{sid}.jsonl"
    _make_transcript(t, sid, "2026-02-15")
    _sweep_transcript(t, palace_path)

    from agent.migrate import run_migration

    stats = run_migration(
        kent_home=kent_home, palace_path=palace_path,
        transcript_base=transcript_base, default_wing="kent_default",
        dry_run=True,
    )

    assert stats["dry_run"] is True
    assert stats["drawers_to_update"] > 0

    # No drawers should actually be tagged
    col = get_collection(str(palace_path), create=False)
    all_data = col.get(include=["metadatas"])
    metas = all_data.get("metadatas") or []
    tagged = [m for m in metas if (m or {}).get("wing")]
    assert not tagged, "dry-run must not write any metadata"
