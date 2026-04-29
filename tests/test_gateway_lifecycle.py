"""Offline tests for agent.gateway.lifecycle (PID file + spawn/stop)."""
from __future__ import annotations

import os
import sys
import time  # noqa: F401


def test_pid_path_uses_kent_home(monkeypatch, tmp_path):
    monkeypatch.setenv("KENT_HOME", str(tmp_path))
    from agent.gateway import lifecycle as lc

    assert lc.pid_path() == tmp_path / "gateway.pid"


def test_read_pid_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("KENT_HOME", str(tmp_path))
    from agent.gateway import lifecycle as lc

    assert lc.read_pid() is None


def test_read_pid_returns_none_when_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("KENT_HOME", str(tmp_path))
    from agent.gateway import lifecycle as lc

    # 99999999 is almost certainly not a live PID
    p = tmp_path / "gateway.pid"
    p.write_text("99999999")

    assert lc.read_pid() is None
    # Stale file should have been removed
    assert not p.exists()


def test_write_then_read_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("KENT_HOME", str(tmp_path))
    from agent.gateway import lifecycle as lc

    own_pid = os.getpid()
    lc.write_pid(own_pid)
    assert lc.read_pid() == own_pid


def test_clear_pid_removes_file(monkeypatch, tmp_path):
    monkeypatch.setenv("KENT_HOME", str(tmp_path))
    from agent.gateway import lifecycle as lc

    lc.write_pid(os.getpid())
    assert lc.pid_path().exists()
    lc.clear_pid()
    assert not lc.pid_path().exists()


def test_spawn_detached_runs_command(tmp_path):
    from agent.gateway import lifecycle as lc

    log_path = tmp_path / "child.log"
    pid = lc.spawn_detached(
        [sys.executable, "-c", "import time; time.sleep(0.3)"],
        log_path,
    )
    assert pid > 0
    # is_alive should be True immediately after spawn (child still running)
    assert lc.is_alive(pid)
    # After child exits, is_alive eventually returns False (reaps the zombie)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not lc.is_alive(pid):
            break
        time.sleep(0.1)
    assert not lc.is_alive(pid)


def test_stop_kills_alive_process(tmp_path):
    from agent.gateway import lifecycle as lc

    log_path = tmp_path / "child.log"
    pid = lc.spawn_detached(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        log_path,
    )
    # Give it a moment to actually start
    time.sleep(0.2)
    assert lc.is_alive(pid)

    assert lc.stop(pid, timeout=5.0) is True
    assert not lc.is_alive(pid)


def test_status_roundtrip_and_clear(monkeypatch, tmp_path):
    """write_status / read_status round-trip; clear_pid also clears status."""
    monkeypatch.setenv("KENT_HOME", str(tmp_path))
    from agent.gateway import lifecycle as lc

    assert lc.read_status() == {}

    payload = {"channels": 7, "ready_at": "2026-04-29T10:00:00+00:00"}
    lc.write_status(payload)
    assert lc.read_status() == payload
    assert lc.status_path().exists()

    # clear_pid should also remove the status file
    lc.write_pid(os.getpid())
    lc.clear_pid()
    assert not lc.pid_path().exists()
    assert not lc.status_path().exists()
    assert lc.read_status() == {}
