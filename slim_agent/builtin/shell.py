"""
Cross-platform shell tool.

Detects the host OS and dispatches commands accordingly:
- macOS / Linux            → /bin/bash -lc <cmd>
- Linux inside WSL         → /bin/bash -lc <cmd>  (same as Linux)
- Windows with WSL present → wsl.exe -- bash -lc <cmd>
- Windows without WSL      → powershell.exe -NoProfile -Command <cmd>

Marked concurrency-unsafe — shell commands can mutate global state.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..events import ToolResult
from ..tools import ToolContext

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_BYTES = 32_000


@dataclass(frozen=True)
class ShellBackend:
    label: str           # human-readable, shown in startup banner
    program: str         # executable
    arg_prefix: tuple[str, ...]  # args before the user command


def _is_wsl_runtime() -> bool:
    """True when *we* are running inside WSL (Linux kernel built by Microsoft)."""
    if platform.system() != "Linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def detect_shell_backend() -> ShellBackend:
    system = platform.system()
    if system == "Darwin":
        return ShellBackend("bash (macOS)", "/bin/bash", ("-lc",))
    if system == "Linux":
        label = "bash (WSL)" if _is_wsl_runtime() else "bash (Linux)"
        return ShellBackend(label, "/bin/bash", ("-lc",))
    if system == "Windows":
        if shutil.which("wsl.exe"):
            return ShellBackend(
                "wsl.exe → bash (Windows + WSL)",
                "wsl.exe",
                ("--", "bash", "-lc"),
            )
        return ShellBackend(
            "PowerShell (Windows)",
            "powershell.exe",
            ("-NoProfile", "-Command"),
        )
    return ShellBackend(f"sh ({system or 'unknown'})", "/bin/sh", ("-c",))


class ShellArgs(BaseModel):
    command: str = Field(min_length=1, description="The shell command to execute")
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        le=600,
        description="Kill the command if it runs longer than this",
    )


def _truncate(text: str) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= MAX_OUTPUT_BYTES:
        return text
    head = raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return f"{head}\n\n<truncated total_bytes={len(raw)}>"


class Shell:
    name = "shell"
    description = (
        "Run a command in the host shell. Backend is auto-detected at startup "
        "(bash on macOS/Linux/WSL, PowerShell on Windows without WSL). Returns "
        "{exit_code, stdout, stderr}. Output is truncated to ~32KB per stream."
    )
    input_model = ShellArgs

    def __init__(self, backend: ShellBackend | None = None):
        self.backend = backend or detect_shell_backend()

    def is_concurrency_safe(self, args: ShellArgs) -> bool:
        return False

    async def call(self, args: ShellArgs, ctx: ToolContext) -> ToolResult:
        proc = await asyncio.create_subprocess_exec(
            self.backend.program,
            *self.backend.arg_prefix,
            args.command,
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
                watch,
                timeout=args.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
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
            reason = "aborted" if (ctx.signal and ctx.signal.is_set()) else "timed out"
            return ToolResult(
                call_id="",
                output=f"Command {reason} after {args.timeout_seconds}s",
                is_error=True,
            )

        stdout_bytes, stderr_bytes = comm_task.result()
        return ToolResult(
            call_id="",
            output={
                "exit_code": proc.returncode,
                "stdout": _truncate(stdout_bytes.decode("utf-8", errors="replace")),
                "stderr": _truncate(stderr_bytes.decode("utf-8", errors="replace")),
                "backend": self.backend.label,
            },
            is_error=proc.returncode != 0,
        )
