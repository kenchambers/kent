"""Tiny terminal UI helpers — ANSI colors + a thinking spinner.

Stdlib-only. Auto-disables on non-tty / NO_COLOR / KENT_NO_COLOR.
"""
from __future__ import annotations

import os
import sys
import threading
import time

# ---------- color detection -------------------------------------------------

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("KENT_NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


_COLOR = _color_enabled()

# 256-color palette (so we get the same look as dev-startup.sh).
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"

CYAN = "\033[38;5;51m"
PURPLE = "\033[38;5;141m"
GOLD = "\033[38;5;220m"
GREEN = "\033[38;5;120m"
RED = "\033[38;5;203m"
GREY = "\033[38;5;244m"
INK = "\033[38;5;39m"
PINK = "\033[38;5;213m"


def c(code: str, text: str) -> str:
    """Wrap text in an ANSI code (no-op when colors disabled)."""
    if not _COLOR:
        return text
    return f"{code}{text}{RESET}"


def style(text: str, *codes: str) -> str:
    """Compose multiple ANSI codes (e.g. bold + color)."""
    if not _COLOR:
        return text
    return f"{''.join(codes)}{text}{RESET}"


# ---------- semantic helpers ------------------------------------------------

def header(label: str) -> str:
    """`[label]` — cyan bold bracket header."""
    return style(f"[{label}]", BOLD, CYAN)


def field(label: str, value: str, *, label_width: int = 11) -> str:
    """Indented `  label : value` line with a dim label."""
    return f"  {c(GREY, label.ljust(label_width))} {value}"


def prompt() -> str:
    """The `you>` prompt — bold cyan."""
    return style("you>", BOLD, CYAN) + " "


def tool_call(name: str, args_preview: str) -> str:
    arrow = c(GREY, "▸")
    n = c(CYAN, name)
    args = c(DIM, args_preview)
    return f"  {arrow} {n}{c(DIM, '(')}{args}{c(DIM, ')')}"


def tool_result(call_id: str, ok: bool) -> str:
    arrow = c(GREY, "◂")
    if ok:
        badge = style(" OK ", BOLD, GREEN)
    else:
        badge = style(" ERR", BOLD, RED)
    return f"  {arrow} {badge} {c(DIM, call_id)}"


def critic_banner(preview: str) -> str:
    head = style("[critic]", BOLD, GOLD)
    return f"\n{head} {c(DIM, 'flagged issues — revising')}\n  {c(GOLD, preview)}"


def error_line(label: str, msg: str) -> str:
    return style(f"[{label}]", BOLD, RED) + " " + c(RED, msg)


def info_line(label: str, msg: str) -> str:
    return style(f"[{label}]", BOLD, GOLD) + " " + msg


def rule(width: int = 60) -> str:
    return c(GREY, "─" * width)


# ---------- spinner ---------------------------------------------------------

class Spinner:
    """Braille-frame spinner that runs in a daemon thread.

    Safe to .stop() and .start() repeatedly. No-ops when colors are disabled
    (so non-tty runs stay clean for piping/CI).
    """

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    INTERVAL = 0.08

    def __init__(self, msg: str = "thinking", color: str = CYAN):
        self.msg = msg
        self.color = color
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, msg: str | None = None) -> "Spinner":
        if not _COLOR:
            return self
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            if msg is not None:
                self.msg = msg
            self._stop.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def _spin(self) -> None:
        i = 0
        try:
            sys.stdout.write("\033[?25l")  # hide cursor
            sys.stdout.flush()
            while not self._stop.is_set():
                frame = self.FRAMES[i]
                sys.stdout.write(
                    f"\r  {c(self.color, frame)} {c(DIM, self.msg)}\033[K"
                )
                sys.stdout.flush()
                i = (i + 1) % len(self.FRAMES)
                time.sleep(self.INTERVAL)
        finally:
            sys.stdout.write("\r\033[K\033[?25h")  # clear line, show cursor
            sys.stdout.flush()

    def stop(self) -> None:
        with self._lock:
            t = self._thread
            self._thread = None
        if t is None:
            return
        self._stop.set()
        t.join(timeout=0.3)
