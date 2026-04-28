"""Tests for palace isolation: hardlinks, SQLite copy, diary copy."""
from __future__ import annotations

from pathlib import Path

from agent.training.palace_isolation import snapshot, cleanup, assert_isolation


def _make_palace(root: Path) -> tuple[Path, Path]:
    kent_home = root / ".kent"
    palace = kent_home / "palace"
    palace.mkdir(parents=True)
    (kent_home / "diaries" / "main").mkdir(parents=True)
    (kent_home / "active_wing.txt").write_text("main\n")

    # Regular palace file (will be hardlinked)
    (palace / "data.bin").write_bytes(b"binary data")
    # SQLite file (must be copied, not hardlinked)
    (palace / "chroma.sqlite3").write_bytes(b"sqlite header")
    # Diary file
    (kent_home / "diaries" / "main" / "2026-01-01.md").write_text("# 2026-01-01\n\n## entry\n")

    return kent_home, palace


def test_snapshot_creates_scratch(tmp_path):
    kent_home, palace = _make_palace(tmp_path)
    rollout_id, scratch = snapshot(kent_home, palace)
    try:
        assert scratch.exists()
        assert (scratch / "palace").exists()
        assert (scratch / "diaries").exists()
        assert (scratch / "active_wing.txt").exists()
    finally:
        cleanup(rollout_id)


def test_sqlite_writes_dont_corrupt_base(tmp_path):
    """chroma.sqlite3 must be copied, not hardlinked — writes must not affect source."""
    kent_home, palace = _make_palace(tmp_path)
    rollout_id, scratch = snapshot(kent_home, palace)
    try:
        src = palace / "chroma.sqlite3"
        dst = scratch / "palace" / "chroma.sqlite3"

        # Verify different inodes
        assert src.stat().st_ino != dst.stat().st_ino, "chroma.sqlite3 must not be hardlinked"

        # Write to scratch copy — must not affect source
        dst.write_bytes(b"modified sqlite")
        assert src.read_bytes() == b"sqlite header"
    finally:
        cleanup(rollout_id)


def test_diary_writes_dont_corrupt_base(tmp_path):
    """Diary files must be copied, not hardlinked — appends must not affect source."""
    kent_home, palace = _make_palace(tmp_path)
    rollout_id, scratch = snapshot(kent_home, palace)
    try:
        src_diary = kent_home / "diaries" / "main" / "2026-01-01.md"
        dst_diary = scratch / "diaries" / "main" / "2026-01-01.md"

        assert src_diary.stat().st_ino != dst_diary.stat().st_ino, "Diary must not be hardlinked"

        # Append to scratch — must not affect source
        with open(dst_diary, "a") as f:
            f.write("\n## new entry\nContent.\n")

        assert "new entry" not in src_diary.read_text()
    finally:
        cleanup(rollout_id)


def test_regular_palace_file_is_hardlinked(tmp_path):
    """Non-sqlite palace files should be hardlinked for speed."""
    kent_home, palace = _make_palace(tmp_path)
    rollout_id, scratch = snapshot(kent_home, palace)
    try:
        src = palace / "data.bin"
        dst = scratch / "palace" / "data.bin"
        assert src.stat().st_ino == dst.stat().st_ino, "Regular palace file should be hardlinked"
    finally:
        cleanup(rollout_id)


def test_cleanup_removes_scratch(tmp_path):
    kent_home, palace = _make_palace(tmp_path)
    rollout_id, scratch = snapshot(kent_home, palace)
    assert scratch.exists()
    cleanup(rollout_id)
    assert not scratch.exists()


def test_assert_isolation_passes_on_valid_snapshot(tmp_path):
    kent_home, palace = _make_palace(tmp_path)
    rollout_id, scratch = snapshot(kent_home, palace)
    try:
        assert_isolation(kent_home, palace, scratch)  # must not raise
    finally:
        cleanup(rollout_id)


def test_parallel_rollouts_dont_corrupt(tmp_path):
    """Two simultaneous snapshots of the same palace must not share mutable state."""
    kent_home, palace = _make_palace(tmp_path)
    id1, scratch1 = snapshot(kent_home, palace)
    id2, scratch2 = snapshot(kent_home, palace)
    try:
        dst1 = scratch1 / "palace" / "chroma.sqlite3"
        dst2 = scratch2 / "palace" / "chroma.sqlite3"
        dst1.write_bytes(b"rollout1 data")
        assert dst2.read_bytes() == b"sqlite header"
    finally:
        cleanup(id1)
        cleanup(id2)
