"""discord_set_status — change the bot's presence and activity."""
from __future__ import annotations

from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext


class DiscordSetStatus:
    """Change the bot's online status and (optionally) activity string."""

    name = "discord_set_status"
    description = (
        "Set the bot's Discord presence: online | idle | dnd | invisible. "
        "Optionally pass activity to update the 'Playing X' / 'thinking' text."
    )

    class Args(BaseModel):
        status: Literal["online", "idle", "dnd", "invisible"]
        activity: str | None = None

    input_model = Args

    def __init__(self, *, bot: Any) -> None:
        self._bot = bot

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        try:
            import discord
        except ImportError:
            return ToolResult(
                call_id="",
                output="discord_set_status requires discord.py; install with: uv pip install discord.py",
                is_error=True,
            )

        status_map = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }
        try:
            activity = discord.Game(name=args.activity) if args.activity else None
            await self._bot.change_presence(
                status=status_map[args.status], activity=activity
            )
        except Exception as e:
            return ToolResult(
                call_id="",
                output=f"discord_set_status failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        activity_part = f" activity={args.activity!r}" if args.activity else ""
        return ToolResult(
            call_id="",
            output=f"[discord_set_status] status={args.status}{activity_part}",
        )
