"""Unit tests for DiaryWriter — mempalace mocked, flock verified."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_ingest(monkeypatch):
    """Mock mempalace.diary_ingest.ingest_diaries and capture calls.

    Restores sys.modules entries on teardown so other tests that import the real
    mempalace package (e.g. tests/test_memory_store.py) don't see a stale MagicMock.
    """
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)

    import sys

    prev_mempalace = sys.modules.get("mempalace")
    prev_diary = sys.modules.get("mempalace.diary_ingest")
    inserted_mempalace = "mempalace" not in sys.modules

    fake_mod = MagicMock()
    fake_mod.ingest_diaries.side_effect = _fake
    if inserted_mempalace:
        sys.modules["mempalace"] = MagicMock()
    sys.modules["mempalace.diary_ingest"] = fake_mod

    yield calls

    if prev_diary is not None:
        sys.modules["mempalace.diary_ingest"] = prev_diary
    else:
        sys.modules.pop("mempalace.diary_ingest", None)
    if inserted_mempalace:
        if prev_mempalace is not None:
            sys.modules["mempalace"] = prev_mempalace
        else:
            sys.modules.pop("mempalace", None)


def _make_writer(tmp_path: Path):
    from agent.memory.diary import DiaryWriter

    return DiaryWriter(palace_path=tmp_path / "palace", kent_home=tmp_path)


def test_diary_file_created_in_wing_dir(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("myproject", "OBSERVATION", "something interesting")
    today = date.today().isoformat()
    expected = tmp_path / "diaries" / "myproject" / f"{today}.md"
    assert expected.exists()


def test_diary_header_format(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "DECISION", "use feature flags")
    today = date.today().isoformat()
    content = (tmp_path / "diaries" / "proj" / f"{today}.md").read_text()
    # Should contain date header
    assert f"# {today}" in content
    # Should contain DECISION kind
    assert "[DECISION]" in content
    assert "[agent=kent]" in content


def test_diary_header_with_topic(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "FINDING", "memory bottleneck", topic="performance")
    today = date.today().isoformat()
    content = (tmp_path / "diaries" / "proj" / f"{today}.md").read_text()
    assert "performance" in content
    assert "[FINDING]" in content


def test_diary_header_timestamp_format(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "OBSERVATION", "test entry")
    today = date.today().isoformat()
    content = (tmp_path / "diaries" / "proj" / f"{today}.md").read_text()
    # Timestamp should match HH:MM:SS
    assert re.search(r"## \d{2}:\d{2}:\d{2}", content)


def test_ingest_called_once_per_write(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "OBSERVATION", "first")
    assert len(mock_ingest) == 1


def test_ingest_called_with_correct_kwargs(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("myproj", "OBSERVATION", "test")
    call = mock_ingest[0]
    assert call["wing"] == "myproj"
    assert call["palace_path"] == str(tmp_path / "palace")
    assert "myproj" in call["diary_dir"]
    assert call["force"] is False


def test_two_writes_append_not_clobber(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "OBSERVATION", "first entry")
    writer.write("proj", "DECISION", "second entry")
    today = date.today().isoformat()
    content = (tmp_path / "diaries" / "proj" / f"{today}.md").read_text()
    assert "first entry" in content
    assert "second entry" in content


def test_date_header_written_only_once(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "OBSERVATION", "a")
    writer.write("proj", "OBSERVATION", "b")
    today = date.today().isoformat()
    content = (tmp_path / "diaries" / "proj" / f"{today}.md").read_text()
    # Date header appears exactly once
    assert content.count(f"# {today}") == 1


def test_ingest_exception_swallowed(tmp_path, monkeypatch):
    """mempalace error during ingest must not propagate to caller."""
    import sys
    from unittest.mock import MagicMock

    prev_mempalace = sys.modules.get("mempalace")
    prev_diary = sys.modules.get("mempalace.diary_ingest")
    inserted_mempalace = "mempalace" not in sys.modules

    fake_mod = MagicMock()
    fake_mod.ingest_diaries.side_effect = RuntimeError("palace exploded")
    if inserted_mempalace:
        sys.modules["mempalace"] = MagicMock()
    sys.modules["mempalace.diary_ingest"] = fake_mod

    try:
        writer = _make_writer(tmp_path)
        writer.write("proj", "OBSERVATION", "should not raise")  # Must not raise
    finally:
        if prev_diary is not None:
            sys.modules["mempalace.diary_ingest"] = prev_diary
        else:
            sys.modules.pop("mempalace.diary_ingest", None)
        if inserted_mempalace:
            if prev_mempalace is not None:
                sys.modules["mempalace"] = prev_mempalace
            else:
                sys.modules.pop("mempalace", None)


def test_ingest_called_twice_for_two_writes(tmp_path, mock_ingest):
    writer = _make_writer(tmp_path)
    writer.write("proj", "OBSERVATION", "a")
    writer.write("proj", "OBSERVATION", "b")
    assert len(mock_ingest) == 2


def test_flock_acquired_and_released(tmp_path, mock_ingest):
    """Verify flock is called (acquired then released) during write."""
    import fcntl

    lock_calls: list[int] = []
    original_flock = fcntl.flock

    def _tracking_flock(f, op):
        lock_calls.append(op)
        return original_flock(f, op)

    with patch("fcntl.flock", side_effect=_tracking_flock):
        writer = _make_writer(tmp_path)
        writer.write("proj", "OBSERVATION", "test")

    assert fcntl.LOCK_EX in lock_calls
    assert fcntl.LOCK_UN in lock_calls
