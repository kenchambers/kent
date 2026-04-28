"""Tests for mtime_signature() — catches the directory-mtime regression (R6).

Directory mtime does NOT change when an existing file's contents are modified.
Per-file stat() is required to detect diary content edits.
"""
import time
from pathlib import Path


class TestMtimeSignatureFilesystem:
    """mtime_signature catches content edits to existing diary files."""

    def test_detects_new_diary_file(self, tmp_path: Path) -> None:
        from agent.viz.server import mtime_signature

        palace = tmp_path / "palace"
        palace.mkdir()
        kent_home = tmp_path / "kent"
        kent_home.mkdir()

        sig_before = mtime_signature(palace, kent_home)

        diaries = kent_home / "diaries" / "wing1"
        diaries.mkdir(parents=True)
        (diaries / "2026-04-28.md").write_text("# entry\n")

        sig_after = mtime_signature(palace, kent_home)
        assert sig_before != sig_after

    def test_detects_content_edit_to_existing_diary(self, tmp_path: Path) -> None:
        """This is the regression R6 catches: directory mtime alone would miss this."""
        from agent.viz.server import mtime_signature

        palace = tmp_path / "palace"
        palace.mkdir()
        kent_home = tmp_path / "kent"
        kent_home.mkdir()

        diaries = kent_home / "diaries" / "wing1"
        diaries.mkdir(parents=True)
        diary_file = diaries / "2026-04-28.md"
        diary_file.write_text("# entry v1\n")

        sig_before = mtime_signature(palace, kent_home)

        # Sleep briefly so mtime changes.
        time.sleep(0.01)
        diary_file.write_text("# entry v1\n## entry v2\n")

        sig_after = mtime_signature(palace, kent_home)
        assert sig_before != sig_after

    def test_stable_when_nothing_changes(self, tmp_path: Path) -> None:
        from agent.viz.server import mtime_signature

        palace = tmp_path / "palace"
        palace.mkdir()
        kent_home = tmp_path / "kent"
        kent_home.mkdir()

        sig1 = mtime_signature(palace, kent_home)
        sig2 = mtime_signature(palace, kent_home)
        assert sig1 == sig2

    def test_detects_sqlite_change(self, tmp_path: Path) -> None:
        from agent.viz.server import mtime_signature

        palace = tmp_path / "palace"
        palace.mkdir()
        kent_home = tmp_path / "kent"
        kent_home.mkdir()

        sig_before = mtime_signature(palace, kent_home)

        time.sleep(0.01)
        (palace / "chroma.sqlite3").write_bytes(b"stub")

        sig_after = mtime_signature(palace, kent_home)
        assert sig_before != sig_after

    def test_missing_files_do_not_raise(self, tmp_path: Path) -> None:
        from agent.viz.server import mtime_signature

        palace = tmp_path / "palace"  # does not exist
        kent_home = tmp_path / "kent"  # does not exist
        # Must not raise FileNotFoundError.
        sig = mtime_signature(palace, kent_home)
        assert isinstance(sig, tuple)
