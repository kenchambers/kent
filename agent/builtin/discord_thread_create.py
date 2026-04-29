"""discord_thread_create — open a new thread in the inbound channel."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext


class DiscordThreadCreate:
    """Create a public thread in the channel."""

    name = "discord_thread_create"
    description = (
        "Open a new public thread in the channel. If parent_message_id is set, "
        "the thread is anchored to that message; otherwise a standalone thread "
        "is opened. auto_archive_minutes accepts 60, 1440 (1d), 4320 (3d), "
        "or 10080 (7d)."
    )

    class Args(BaseModel):
        name: str
        parent_message_id: int | None = None
        auto_archive_minutes: int = 1440

    input_model = Args

    def __init__(self, *, bot: Any, default_channel_id: int) -> None:
        self._bot = bot
        self._default_channel_id = default_channel_id

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        try:
            import discord  # noqa: F401
        except ImportError:
            return ToolResult(
                call_id="",
                output="discord_thread_create requires discord.py; install with: uv pip install discord.py",
                is_error=True,
            )

        cid = self._default_channel_id
        channel = self._bot.get_channel(cid)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(cid)
            except Exception as e:
                return ToolResult(
                    call_id="",
                    output=f"discord_thread_create: channel {cid} not found ({type(e).__name__}: {e})",
                    is_error=True,
                )

        try:
            if args.parent_message_id is not None:
                anchor = await channel.fetch_message(args.parent_message_id)
                thread = await anchor.create_thread(
                    name=args.name,
                    auto_archive_duration=args.auto_archive_minutes,
                )
            else:
                thread = await channel.create_thread(
                    name=args.name,
                    auto_archive_duration=args.auto_archive_minutes,
                )
        except Exception as e:
            return ToolResult(
                call_id="",
                output=f"discord_thread_create failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        thread_id = getattr(thread, "id", "?")
        return ToolResult(
            call_id="",
            output=f"[discord_thread_create] name={args.name!r} thread_id={thread_id}",
        )
