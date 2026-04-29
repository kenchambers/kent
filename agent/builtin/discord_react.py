"""discord_react — add a reaction emoji to a Discord message."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext


class DiscordReact:
    """Add a reaction to a Discord message."""

    name = "discord_react"
    description = (
        "Add a reaction emoji to a Discord message. Pass the message_id and the "
        "emoji as a unicode character (e.g. '🔥') or a Discord custom-emoji form "
        "(':name:id'). channel_id defaults to the inbound channel."
    )

    class Args(BaseModel):
        message_id: int
        emoji: str
        channel_id: int | None = None

    input_model = Args

    def __init__(self, *, bot: Any, default_channel_id: int) -> None:
        self._bot = bot
        self._default_channel_id = default_channel_id

    def is_concurrency_safe(self, args: Args) -> bool:
        return True

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        try:
            import discord  # noqa: F401
        except ImportError:
            return ToolResult(
                call_id="",
                output="discord_react requires discord.py; install with: uv pip install discord.py",
                is_error=True,
            )

        cid = args.channel_id or self._default_channel_id
        channel = self._bot.get_channel(cid)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(cid)
            except Exception as e:
                return ToolResult(
                    call_id="",
                    output=f"discord_react: channel {cid} not found ({type(e).__name__}: {e})",
                    is_error=True,
                )
        try:
            message = await channel.fetch_message(args.message_id)
            await message.add_reaction(args.emoji)
        except Exception as e:
            return ToolResult(
                call_id="",
                output=f"discord_react failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolResult(
            call_id="",
            output=f"[discord_react] message={args.message_id} emoji={args.emoji}",
        )
