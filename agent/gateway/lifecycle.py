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
    """Spawn argv as a fully-detached daemon; return the daemon's PID.

    On POSIX uses a true double-fork (fork → setsid → fork → exec) so the
    daemon survives termination of the original session — the WSL2 +
    short-lived-shell case where systemd's user-session cleanup would
    otherwise reap orphans of a one-shot bash invocation. The grandchild
    becomes a child of init/PID 1, severs its controlling terminal, and is
    no longer in any cgroup tied to the originating login session.

    On platforms without ``os.fork`` (Windows), falls back to
    ``subprocess.Popen(start_new_session=True)`` — the prior behavior.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not hasattr(os, "fork"):
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

    # Pipe carries the grandchild PID (or an error string) back to the caller.
    r_fd, w_fd = os.pipe()
    intermediate_pid = os.fork()

    if intermediate_pid > 0:
        # Original process: reap intermediate, read daemon PID, return it.
        os.close(w_fd)
        try:
            os.waitpid(intermediate_pid, 0)
        except ChildProcessError:
            pass
        buf = b""
        while True:
            try:
                chunk = os.read(r_fd, 64)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        os.close(r_fd)
        text = buf.decode("ascii", "replace").strip()
        if text.startswith("ERROR:"):
            raise RuntimeError(f"daemon failed to start: {text[6:].strip()}")
        try:
            return int(text)
        except ValueError:
            raise RuntimeError(f"daemon failed to start (no pid reported): {text!r}")

    # Intermediate process: setsid, fork the daemon, exit.
    try:
        os.close(r_fd)
        os.setsid()
        daemon_pid = os.fork()
        if daemon_pid > 0:
            try:
                os.write(w_fd, str(daemon_pid).encode())
            finally:
                os.close(w_fd)
            os._exit(0)

        # Grandchild: become the daemon. Redirect stdio, close inherited fds,
        # exec the target. Past this point we must os._exit on any failure —
        # never raise, never return to Python's caller.
        try:
            os.close(w_fd)
            null_fd = os.open(os.devnull, os.O_RDONLY)
            log_fd = os.open(
                str(log_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o644,
            )
            os.dup2(null_fd, 0)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.close(null_fd)
            os.close(log_fd)
            try:
                os.closerange(3, 1024)
            except OSError:
                pass
            os.execvp(argv[0], argv)
        except BaseException as e:
            try:
                with open(log_path, "ab") as fh:
                    fh.write(f"[lifecycle] exec failed: {e}\n".encode())
            except OSError:
                pass
            os._exit(1)
    except BaseException as e:
        try:
            os.write(w_fd, f"ERROR: {type(e).__name__}: {e}".encode())
        except OSError:
            pass
        os._exit(1)


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
