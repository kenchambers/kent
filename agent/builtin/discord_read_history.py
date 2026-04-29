"""discord_read_history — read recent messages in a Discord channel."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext


class DiscordReadHistory:
    """Read recent messages in a Discord channel (chronological)."""

    name = "discord_read_history"
    description = (
        "Read recent messages in a Discord channel. Returns oldest-first text "
        "with author + content. channel_id defaults to the inbound channel."
    )

    class Args(BaseModel):
        limit: int = 50
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
                output="discord_read_history requires discord.py; install with: uv pip install discord.py",
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
                    output=f"discord_read_history: channel {cid} not found ({type(e).__name__}: {e})",
                    is_error=True,
                )

        limit = max(1, min(args.limit, 200))
        try:
            lines: list[str] = []
            async for msg in channel.history(limit=limit, oldest_first=True):
                author = getattr(msg.author, "display_name", None) or getattr(
                    msg.author, "name", "?"
                )
                content = (msg.content or "").replace("\n", " ")
                lines.append(f"[{msg.id}] {author}: {content}")
        except Exception as e:
            return ToolResult(
                call_id="",
                output=f"discord_read_history failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolResult(
            call_id="",
            output={"channel_id": cid, "messages": lines},
        )
