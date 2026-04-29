"""
Cross-platform shell tool.

Backend selection is handled by ``_executors.default_executor()``:
- macOS / Linux            → BashExecutor
- Linux inside WSL         → BashExecutor (labelled "bash (WSL)")
- Windows with WSL present → WSLExecutor (wsl.exe -- bash -lc)
- Windows without WSL      → PowerShellExecutor

Optional per-call args let the agent target a specific WSL distro/user or
escalate via ElevatedExecutor (sudo on POSIX; requires-admin on Windows).

Marked concurrency-unsafe — shell commands can mutate global state.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..events import ToolResult
from ..tools import ToolContext
from ._executors import (
    BashExecutor,
    ElevatedExecutor,
    PowerShellExecutor,
    ShellExecutor,
    WSLExecutor,
    default_executor,
)

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_BYTES = 32_000


@dataclass(frozen=True)
class ShellBackend:
    """Legacy descriptor kept for ``detect_shell_backend()`` callers and tests."""

    label: str
    program: str
    arg_prefix: tuple[str, ...]


def detect_shell_backend() -> ShellBackend:
    """Return a ShellBackend describing the default executor's argv shape."""
    ex = default_executor()
    sample = ex.build_argv("__CMD__")
    program = sample[0]
    try:
        prefix = tuple(sample[1 : sample.index("__CMD__")])
    except ValueError:
        prefix = tuple(sample[1:])
    return ShellBackend(label=ex.label, program=program, arg_prefix=prefix)


class ShellArgs(BaseModel):
    command: str = Field(min_length=1, description="The shell command to execute")
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        le=600,
        description="Kill the command if it runs longer than this",
    )
    distro: str | None = Field(
        default=None,
        description="WSL distro to target (Windows + WSL only). Ignored elsewhere.",
    )
    user: str | None = Field(
        default=None,
        description="User to run as inside WSL (e.g. 'root'). Ignored elsewhere.",
    )
    elevate: bool = Field(
        default=False,
        description=(
            "Run with elevated privileges. POSIX uses 'sudo -n'; Windows requires "
            "the agent to already be running as Administrator."
        ),
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
        "(bash on macOS/Linux/WSL, PowerShell on Windows without WSL). On Windows "
        "with WSL, optional 'distro'/'user' target a specific environment, and "
        "'elevate' escalates privileges. Returns {exit_code, stdout, stderr}. "
        "Output is truncated to ~32KB per stream."
    )
    input_model = ShellArgs

    def __init__(self, executor: ShellExecutor | None = None):
        self._default = executor or default_executor()

    def is_concurrency_safe(self, args: ShellArgs) -> bool:
        return False

    def _resolve_executor(self, args: ShellArgs) -> tuple[ShellExecutor, list[str]]:
        """Return (executor, warnings). Warnings list any args that were ignored."""
        ex = self._default
        warnings: list[str] = []
        if args.distro or args.user:
            if isinstance(ex, WSLExecutor):
                ex = WSLExecutor(
                    distro=args.distro or ex.distro,
                    user=args.user or ex.user,
                )
            else:
                warnings.append(
                    f"distro/user ignored: active backend is {ex.label!r}, not WSL"
                )
        if args.elevate:
            ex = ElevatedExecutor(ex)
        return ex, warnings

    async def call(self, args: ShellArgs, ctx: ToolContext) -> ToolResult:
        try:
            executor, warnings = self._resolve_executor(args)
        except (ValueError, PermissionError) as e:
            return ToolResult(call_id="", output=str(e), is_error=True)

        result = await executor.run(args.command, args.timeout_seconds, ctx)

        if result.aborted or result.timed_out:
            reason = "aborted" if result.aborted else "timed out"
            return ToolResult(
                call_id="",
                output=f"Command {reason} after {args.timeout_seconds}s",
                is_error=True,
            )

        output: dict = {
            "exit_code": result.exit_code,
            "stdout": _truncate(result.stdout),
            "stderr": _truncate(result.stderr),
            "backend": result.backend_label,
        }
        if warnings:
            output["warnings"] = warnings
        return ToolResult(call_id="", output=output, is_error=result.exit_code != 0)


__all__ = [
    "Shell",
    "ShellArgs",
    "ShellBackend",
    "detect_shell_backend",
    "BashExecutor",
    "WSLExecutor",
    "PowerShellExecutor",
    "ElevatedExecutor",
    "ShellExecutor",
]
