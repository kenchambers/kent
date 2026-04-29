"""kent's Discord gateway runtime.

Hosts a discord.py Bot client that dispatches inbound messages to per-channel
ChannelSession objects. Each session owns its own MemPalaceStore (so wing
isolation is preserved across concurrent channels) and its own ToolRegistry
with Discord tools bound to that channel.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from . import lifecycle as _lc
from ..events import AssistantMessageComplete, Terminal
from ..llm import OpenAICompatibleLLM
from ..loop import run as agent_run
from ..tools import ToolRegistry

if TYPE_CHECKING:
    from ..memory.mempalace_store import MemPalaceStore

logger = logging.getLogger(__name__)


DISCORD_SUFFIX = (
    "You are speaking on Discord. Each channel/DM has its own memory wing. "
    "Use the discord_send, discord_react, discord_thread_create, "
    "discord_set_status, and discord_read_history tools to interact. "
    "Replies are split automatically; keep individual messages reasonable. "
    "When the conversation moves to a new topic, create a thread."
)


@dataclass
class DiscordSettings:
    mention_only: bool = True
    status: str = "online"
    activity: str | None = "thinking"
    log_file: Path | None = None
    heartbeat_interval: str | None = None   # "30m", "off", or None (= off)
    heartbeat_channel_id: int | None = None  # channel the heartbeat runs against


@dataclass
class ChannelSession:
    wing: str
    history: list[dict] = field(default_factory=list)
    store: "MemPalaceStore | None" = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    registry: ToolRegistry | None = None


def _wing_for_channel(message: Any) -> str:
    """Return a flat wing name for the message's channel/DM. Length ≤ 64."""
    guild = getattr(message, "guild", None)
    if guild is not None:
        name = f"discord_{guild.id}_{message.channel.id}"
    else:
        author = message.author
        user_id = getattr(author, "id", 0)
        name = f"discord_dm_{user_id}"
    if len(name) > 64:
        name = name[:64]
    return name


def _split_for_discord(text: str, limit: int = 1900) -> list[str]:
    """Split text for Discord's 2000-char limit, preferring word/line boundaries."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        piece = remaining[:cut].rstrip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [text[:limit]]


def _build_discord_registry(
    bot: Any,
    default_channel_id: int,
    *,
    llm: Any,
    memory_store: "MemPalaceStore",
) -> ToolRegistry:
    """Build a ToolRegistry containing standard kent tools + Discord tools."""
    from ..builtin.diary_write import DiaryWrite
    from ..builtin.discord_react import DiscordReact
    from ..builtin.discord_read_history import DiscordReadHistory
    from ..builtin.discord_send import DiscordSend
    from ..builtin.discord_set_status import DiscordSetStatus
    from ..builtin.discord_thread_create import DiscordThreadCreate
    from ..builtin.memory_recall import MemoryRecall
    from ..builtin.memory_recall_here import MemoryRecallHere
    from ..builtin.set_wing import SetWing
    from ..builtin.shell import Shell
    from ..builtin.spawn import Spawn
    from ..builtin.tunnel_create import TunnelCreate
    from ..builtin.web_fetch import WebFetch
    from ..builtin.web_search import WebSearch

    registry = ToolRegistry()
    registry.register(WebSearch())
    registry.register(WebFetch())
    registry.register(Shell())
    registry.register(Spawn(parent_registry=registry, llm=llm, memory_store=memory_store))
    registry.register(MemoryRecall(memory_store))
    registry.register(MemoryRecallHere(memory_store))
    registry.register(DiaryWrite(memory_store))
    registry.register(SetWing(memory_store))
    registry.register(TunnelCreate())

    registry.register(DiscordSend(bot=bot, default_channel_id=default_channel_id))
    registry.register(DiscordReact(bot=bot, default_channel_id=default_channel_id))
    registry.register(DiscordThreadCreate(bot=bot, default_channel_id=default_channel_id))
    registry.register(DiscordSetStatus(bot=bot))
    registry.register(DiscordReadHistory(bot=bot, default_channel_id=default_channel_id))
    return registry


class DiscordGateway:
    """Discord client wrapping a discord.py commands.Bot."""

    def __init__(
        self,
        *,
        token: str,
        settings: DiscordSettings,
        llm_factory: Callable[[], Any],
        system_prompt_factory: Callable[["MemPalaceStore"], str],
        store_factory: Callable[[], "MemPalaceStore"] | None = None,
        wing_override: str | None = None,
    ) -> None:
        import discord
        from discord.ext import commands

        self._token = token
        self._settings = settings
        self._llm_factory = llm_factory
        self._system_prompt_factory = system_prompt_factory
        self._store_factory = store_factory or self._default_store_factory
        self._wing_override = wing_override
        self._sessions: dict[int, ChannelSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._llm = llm_factory()
        self._ready_at: str | None = None
        self._heartbeat: "Any" = None  # Heartbeat | None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True

        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._bot.event(self.on_ready)
        self._bot.event(self.on_message)

    @staticmethod
    def _default_store_factory() -> "MemPalaceStore":
        from ..memory.mempalace_store import MemPalaceStore
        return MemPalaceStore()

    async def on_ready(self) -> None:  # noqa: D401 — discord.py event signature
        import discord

        try:
            status_map = {
                "online": discord.Status.online,
                "idle": discord.Status.idle,
                "dnd": discord.Status.dnd,
                "invisible": discord.Status.invisible,
            }
            status = status_map.get(self._settings.status, discord.Status.online)
            activity = (
                discord.Game(name=self._settings.activity)
                if self._settings.activity
                else None
            )
            await self._bot.change_presence(status=status, activity=activity)
        except Exception:
            logger.warning("on_ready: change_presence failed", exc_info=True)
        user = getattr(self._bot, "user", None)
        if user is not None:
            print(f"[gateway] ready as {user} (id={user.id})", flush=True)
        if self._ready_at is None:
            self._ready_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write_status_snapshot()
        self._start_heartbeat()

    def _write_status_snapshot(self) -> None:
        user = getattr(self._bot, "user", None)
        _lc.write_status(
            {
                "ready_at": self._ready_at,
                "user": str(user) if user is not None else None,
                "user_id": getattr(user, "id", None),
                "channels": len(self._sessions),
                "mention_only": self._settings.mention_only,
                "status": self._settings.status,
            }
        )

    async def _session_for_channel_id(
        self, cid: int, wing_name: str | None = None
    ) -> ChannelSession:
        async with self._sessions_lock:
            sess = self._sessions.get(cid)
            if sess is not None:
                return sess
            wing = self._wing_override or wing_name or f"discord_channel_{cid}"
            store = self._store_factory()
            try:
                store.set_active_wing(wing)
            except ValueError:
                logger.warning("invalid wing %r — falling back to default", wing)
            registry = _build_discord_registry(
                self._bot, cid, llm=self._llm, memory_store=store
            )
            sess = ChannelSession(wing=wing, store=store, registry=registry)
            self._sessions[cid] = sess
            self._write_status_snapshot()
            return sess

    async def _session_for(self, message: Any) -> ChannelSession:
        cid = int(message.channel.id)
        wing_name = _wing_for_channel(message)
        return await self._session_for_channel_id(cid, wing_name=wing_name)

    def _should_respond(self, message: Any) -> bool:
        if message.author.id == self._bot.user.id:
            return False
        if getattr(message.author, "bot", False):
            return False
        if self._settings.mention_only:
            return self._bot.user in message.mentions
        return True

    async def on_message(self, message: Any) -> None:
        try:
            if self._bot.user is None:
                return
            if not self._should_respond(message):
                return
            session = await self._session_for(message)
            async with session.lock:
                await self._handle_turn(session, message)
        except Exception:
            logger.exception("on_message failed")

    async def _handle_turn(self, session: ChannelSession, message: Any) -> None:
        assert session.store is not None and session.registry is not None
        user_text = message.content or ""
        if self._bot.user is not None:
            user_text = user_text.replace(f"<@{self._bot.user.id}>", "").strip()
        if not user_text:
            return

        system_prompt = self._system_prompt_factory(session.store) + "\n\n" + DISCORD_SUFFIX

        history = [*session.history, {"role": "user", "content": user_text}]
        new_messages: list[dict] = []
        final_text = ""

        try:
            async with message.channel.typing():
                async for ev in agent_run(
                    messages=history,
                    tools=session.registry,
                    llm=self._llm,
                    system=system_prompt,
                    max_turns=15,
                    memory_store=session.store,
                ):
                    if isinstance(ev, AssistantMessageComplete):
                        new_messages.append(ev.message.to_openai_dict())
                        if ev.message.content:
                            final_text = ev.message.content
                    elif isinstance(ev, Terminal):
                        break
        except Exception:
            logger.exception("agent run failed")
            try:
                await message.channel.send(
                    "[gateway] sorry — internal error processing that message."
                )
            except Exception:
                pass
            return

        session.history = [*history, *new_messages]

        if final_text.strip():
            for chunk in _split_for_discord(final_text):
                if chunk:
                    try:
                        await message.channel.send(chunk)
                    except Exception:
                        logger.exception("final reply send failed")
                        break

    def _start_heartbeat(self) -> None:
        from .heartbeat import Heartbeat, parse_interval
        interval_str = self._settings.heartbeat_interval or ""
        interval_s = parse_interval(interval_str)
        cid = self._settings.heartbeat_channel_id
        if interval_s and interval_s > 0 and cid:
            logger.info(
                "heartbeat enabled: interval=%.0fs channel=%d", interval_s, cid
            )
            hb = Heartbeat(
                gateway=self,
                interval_s=interval_s,
                channel_id=cid,
            )
            self._heartbeat = hb.start()
        else:
            logger.debug("heartbeat disabled (interval=%r channel=%r)", interval_str, cid)

    async def _run_heartbeat_turn(self, channel_id: int, prompt_text: str) -> None:
        session = await self._session_for_channel_id(channel_id)
        assert session.store is not None and session.registry is not None
        system_prompt = self._system_prompt_factory(session.store) + "\n\n" + DISCORD_SUFFIX
        history = [*session.history, {"role": "user", "content": prompt_text}]
        new_messages: list[dict] = []

        async with session.lock:
            try:
                async for ev in agent_run(
                    messages=history,
                    tools=session.registry,
                    llm=self._llm,
                    system=system_prompt,
                    max_turns=15,
                    memory_store=session.store,
                ):
                    if isinstance(ev, AssistantMessageComplete):
                        new_messages.append(ev.message.to_openai_dict())
                    elif isinstance(ev, Terminal):
                        break
            except Exception:
                logger.exception("heartbeat agent run failed")
                return

        session.history = [*history, *new_messages]

    async def start(self) -> None:
        await self._bot.start(self._token)

    async def close(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):
                pass
        await self._bot.close()


async def run_gateway(
    *,
    token: str,
    settings: DiscordSettings,
    llm_factory: Callable[[], Any],
    system_prompt_factory: Callable[["MemPalaceStore"], str],
    wing_override: str | None = None,
) -> None:
    """Foreground entry point — runs until the connection drops or is killed."""
    gateway = DiscordGateway(
        token=token,
        settings=settings,
        llm_factory=llm_factory,
        system_prompt_factory=system_prompt_factory,
        wing_override=wing_override,
    )
    try:
        await gateway.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await gateway.close()
        raise


__all__ = [
    "DiscordGateway",
    "DiscordSettings",
    "ChannelSession",
    "DISCORD_SUFFIX",
    "_split_for_discord",
    "_wing_for_channel",
    "run_gateway",
]


# Silence unused-import warning for OpenAICompatibleLLM (kept for re-export friendliness)
_ = OpenAICompatibleLLM
