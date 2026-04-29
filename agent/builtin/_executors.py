"""
Shell execution backends — OO scaffold.

Hierarchy:
    ShellExecutor (ABC)
      ├─ BashExecutor          POSIX bash (macOS / native Linux / inside-WSL)
      ├─ WSLExecutor           Windows host → wsl.exe -d <distro> -u <user>
      └─ PowerShellExecutor    Windows host without WSL

Decorator:
    ElevatedExecutor   wraps any executor; escalates via sudo (POSIX) or
                       requires-admin pre-check (Windows). Soft-imports pyuac.

Each concrete executor is constructed once at startup and reused per call.
The ABC owns process lifecycle (spawn, timeout, signal-abort, output capture);
subclasses only assemble argv.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..tools import ToolContext


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    backend_label: str
    aborted: bool = False
    timed_out: bool = False


class ShellExecutor(ABC):
    """Abstract shell backend. Subclasses build argv; base class drives the process."""

    label: str  # populated by subclass

    @abstractmethod
    def build_argv(self, command: str) -> list[str]:
        """Return the full argv (program + args) used to invoke ``command``."""

    def supports_elevation(self) -> bool:
        """Override to advertise that this backend can be wrapped by ElevatedExecutor."""
        return True

    async def run(self, command: str, timeout: float, ctx: ToolContext) -> ExecResult:
        argv = self.build_argv(command)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        comm_task = asyncio.create_task(proc.communicate())
        watch: set[asyncio.Task] = {comm_task}
        signal_task: asyncio.Task | None = None
        if ctx.signal is not None:
            signal_task = asyncio.create_task(ctx.signal.wait())
            watch.add(signal_task)

        try:
            done, _ = await asyncio.wait(
                watch, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if signal_task is not None and not signal_task.done():
                signal_task.cancel()

        if comm_task not in done:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            try:
                comm_task.cancel()
                await comm_task
            except (asyncio.CancelledError, Exception):
                pass
            aborted = bool(ctx.signal and ctx.signal.is_set())
            return ExecResult(
                exit_code=None,
                stdout="",
                stderr="",
                backend_label=self.label,
                aborted=aborted,
                timed_out=not aborted,
            )

        stdout_bytes, stderr_bytes = comm_task.result()
        return ExecResult(
            exit_code=proc.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            backend_label=self.label,
        )


class BashExecutor(ShellExecutor):
    """POSIX bash — macOS, native Linux, or running *inside* a WSL distro."""

    def __init__(self, label: str = "bash", program: str = "/bin/bash"):
        self.label = label
        self.program = program

    def build_argv(self, command: str) -> list[str]:
        return [self.program, "-lc", command]


class PowerShellExecutor(ShellExecutor):
    """Windows host fallback when WSL is unavailable."""

    def __init__(self, program: str = "powershell.exe"):
        self.label = "PowerShell (Windows)"
        self.program = program

    def build_argv(self, command: str) -> list[str]:
        return [self.program, "-NoProfile", "-Command", command]


class WSLExecutor(ShellExecutor):
    """
    Windows → WSL bridge via ``wsl.exe``. Optional distro and user pinning lets
    the agent target a specific environment (e.g. ``-d Ubuntu-22.04 -u root``).
    """

    def __init__(
        self,
        distro: str | None = None,
        user: str | None = None,
        program: str = "wsl.exe",
    ):
        self.distro = distro
        self.user = user
        self.program = program
        suffix = []
        if distro:
            suffix.append(f"distro={distro}")
        if user:
            suffix.append(f"user={user}")
        self.label = "wsl.exe → bash" + (f" ({', '.join(suffix)})" if suffix else "")

    def build_argv(self, command: str) -> list[str]:
        argv: list[str] = [self.program]
        if self.distro:
            argv += ["-d", self.distro]
        if self.user:
            argv += ["-u", self.user]
        argv += ["--", "bash", "-lc", command]
        return argv


class ElevatedExecutor(ShellExecutor):
    """
    Decorator that escalates privileges before delegating to an inner executor.

    POSIX:   prepends ``sudo -n`` (non-interactive — fails fast if a password
             would be required; configure NOPASSWD in sudoers for the agent).
    Windows: requires the parent process to already be admin (verified via
             ``ctypes.windll.shell32.IsUserAnAdmin``). Re-launching mid-call
             with UAC is intentionally not supported because it loses stdio.
             Use ``pyuac.runAsAdmin()`` at process start instead.
    """

    def __init__(self, inner: ShellExecutor):
        if not inner.supports_elevation():
            raise ValueError(f"{type(inner).__name__} does not support elevation")
        self.inner = inner
        self.label = f"elevated[{inner.label}]"

    def supports_elevation(self) -> bool:
        return False  # don't allow double-wrapping

    def build_argv(self, command: str) -> list[str]:
        if platform.system() == "Windows":
            self._require_windows_admin()
            return self.inner.build_argv(command)
        # POSIX: rebuild inner argv but inject sudo before the program
        inner_argv = self.inner.build_argv(command)
        return ["sudo", "-n", *inner_argv]

    @staticmethod
    def _require_windows_admin() -> None:
        try:
            import ctypes  # stdlib, available everywhere

            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            is_admin = False
        if not is_admin:
            raise PermissionError(
                "Elevation requested but the agent process is not running as "
                "Administrator. Relaunch with admin rights (e.g. via "
                "`pyuac.runAsAdmin()` at startup) before using ElevatedExecutor."
            )


def _is_wsl_runtime() -> bool:
    """True when *we* are running inside a WSL distro."""
    if platform.system() != "Linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def default_executor() -> ShellExecutor:
    """Pick the right concrete executor for the current host."""
    system = platform.system()
    if system == "Darwin":
        return BashExecutor(label="bash (macOS)")
    if system == "Linux":
        label = "bash (WSL)" if _is_wsl_runtime() else "bash (Linux)"
        return BashExecutor(label=label)
    if system == "Windows":
        if shutil.which("wsl.exe"):
            return WSLExecutor()
        return PowerShellExecutor()
    return BashExecutor(label=f"sh ({system or 'unknown'})", program="/bin/sh")
