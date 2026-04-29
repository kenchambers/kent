"""Periodic heartbeat: fires one agent turn per tick on a configurable cadence."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import lifecycle as _lc

if TYPE_CHECKING:
    from .discord_bot import DiscordGateway

logger = logging.getLogger(__name__)

_OFF_TOKENS = {"", "off", "disabled", "0", "none"}


def parse_interval(s: str) -> float | None:
    """Parse '30s'/'5m'/'30m'/'1h' → seconds; off-tokens → None."""
    s = s.strip().lower()
    if s in _OFF_TOKENS:
        return None
    try:
        if s.endswith("h"):
            v = float(s[:-1]) * 3600
        elif s.endswith("m"):
            v = float(s[:-1]) * 60
        elif s.endswith("s"):
            v = float(s[:-1])
        else:
            v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _default_md_path() -> Path:
    return _lc._kent_home() / "HEARTBEAT.md"


def read_heartbeat_md(path: Path | None = None) -> str:
    """Read HEARTBEAT.md; return '' if missing so the caller can skip the tick."""
    p = path or _default_md_path()
    try:
        return p.read_text()
    except FileNotFoundError:
        return ""
    except OSError as e:
        logger.warning("heartbeat: could not read %s: %s", p, e)
        return ""


def default_heartbeat_md_text() -> str:
    return (
        "You are the kent agent running a scheduled heartbeat check-in.\n\n"
        "Things you could do on each tick:\n"
        "- Post a brief status update in the channel.\n"
        "- Review recent diary entries and reflect on patterns.\n"
        "- Reach out in a channel if something needs attention.\n"
        "- Run a quick web search on a topic of interest.\n\n"
        "Use your tools (discord_send, diary_write, web_search, etc.) to act. "
        "Keep it brief — this is a background pulse, not a full session.\n"
    )


class Heartbeat:
    def __init__(
        self,
        *,
        gateway: "DiscordGateway",
        interval_s: float,
        channel_id: int,
        md_path: Path | None = None,
    ) -> None:
        self._gateway = gateway
        self._interval_s = interval_s
        self._channel_id = channel_id
        self._md_path = md_path or _default_md_path()
        self._task: asyncio.Task | None = None
        self._warned_missing = False

    def start(self) -> "asyncio.Task[None]":
        self._task = asyncio.get_event_loop().create_task(self._loop())
        return self._task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self._tick()
            except Exception:
                logger.exception("heartbeat tick raised unexpectedly")

    async def _tick(self) -> None:
        prompt_text = read_heartbeat_md(self._md_path)
        if not prompt_text.strip():
            if not self._warned_missing:
                logger.warning(
                    "heartbeat: HEARTBEAT.md missing or empty at %s — skipping tick",
                    self._md_path,
                )
                self._warned_missing = True
            return
        self._warned_missing = False

        try:
            await self._gateway._run_heartbeat_turn(self._channel_id, prompt_text)
            status = "ok"
        except Exception:
            logger.exception("heartbeat turn failed (channel %d)", self._channel_id)
            status = "error"

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _lc.write_status({
            **_lc.read_status(),
            "last_heartbeat_at": ts,
            "last_heartbeat_status": status,
        })
