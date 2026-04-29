"""PID file + detached-process helpers for `kent gateway start/stop/status`."""
from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def _kent_home() -> Path:
    return Path(os.environ.get("KENT_HOME", str(Path.home() / ".kent")))


def pid_path() -> Path:
    return _kent_home() / "gateway.pid"


def status_path() -> Path:
    return _kent_home() / "gateway.status.json"


def write_status(payload: dict) -> None:
    """Atomically write the gateway status JSON. Failures are swallowed."""
    p = status_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, p)
    except OSError:
        pass


def read_status() -> dict:
    """Return the stored status payload, or {} if missing/unreadable."""
    p = status_path()
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def clear_status() -> None:
    p = status_path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # If the pid is one of our children, opportunistically reap so a zombie
    # doesn't masquerade as a live process. We swallow ECHILD for foreign pids.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def read_pid() -> int | None:
    """Return the running pid, or None if missing/stale. Removes stale files."""
    p = pid_path()
    if not p.exists():
        return None
    try:
        text = p.read_text().strip()
        pid = int(text)
    except (OSError, ValueError):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    if not is_alive(pid):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    return pid


def write_pid(pid: int) -> None:
    p = pid_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(pid))


def clear_pid() -> None:
    p = pid_path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    clear_status()


def spawn_detached(argv: list[str], log_path: Path) -> int:
    """Spawn argv as a detached child; return the child PID."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "ab")
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def stop(pid: int, timeout: float = 10.0) -> bool:
    """Send SIGTERM, then SIGKILL if needed. Returns True if the process exited."""
    if not is_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return not is_alive(pid)
