"""discord_send — post a message to a Discord channel."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext


class DiscordSend:
    """Post a message to a Discord channel (chunked at 1900 chars)."""

    name = "discord_send"
    description = (
        "Send a message to a Discord channel. Defaults to the channel of the "
        "incoming message. Pass channel_id to target another channel. "
        "Pass reply_to (a message id) to make this a reply. Long content is "
        "split at word boundaries into multiple ≤1900-char messages."
    )

    class Args(BaseModel):
        content: str
        channel_id: int | None = None
        reply_to: int | None = None

    input_model = Args

    def __init__(self, *, bot: Any, default_channel_id: int) -> None:
        self._bot = bot
        self._default_channel_id = default_channel_id

    def is_concurrency_safe(self, args: Args) -> bool:
        return True

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        from ..gateway.discord_bot import _split_for_discord

        try:
            import discord  # noqa: F401
        except ImportError:
            return ToolResult(
                call_id="",
                output="discord_send requires discord.py; install with: uv pip install discord.py",
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
                    output=f"discord_send: channel {cid} not found ({type(e).__name__}: {e})",
                    is_error=True,
                )

        chunks = _split_for_discord(args.content)
        reference = None
        if args.reply_to is not None:
            try:
                ref_msg = await channel.fetch_message(args.reply_to)
                reference = ref_msg
            except Exception:
                reference = None

        sent = 0
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            try:
                if i == 0 and reference is not None:
                    await channel.send(chunk, reference=reference)
                else:
                    await channel.send(chunk)
                sent += 1
            except Exception as e:
                return ToolResult(
                    call_id="",
                    output=f"discord_send: send failed after {sent} of {len(chunks)} ({type(e).__name__}: {e})",
                    is_error=True,
                )

        return ToolResult(
            call_id="",
            output=f"[discord_send] channel={cid} messages_sent={sent}",
        )
