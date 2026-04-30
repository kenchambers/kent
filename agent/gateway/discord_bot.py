"""kent's Discord gateway runtime.

Hosts a discord.py Bot client that dispatches inbound messages to per-channel
ChannelSession objects. Each session owns its own MemPalaceStore (so wing
isolation is preserved across concurrent channels) and its own ToolRegistry
with Discord tools bound to that channel.
"""
from __future__ import annotations

import asyncio
import json
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


def _missing_msgs_file(store_path: Path) -> Path:
    """Path to the per-gateway missing-messages tracking file."""
    return store_path / "gateway_missing_msgs.json"


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
    # Track the Discord message ID we most recently responded to in this channel.
    # Used by the post-ready gap scanner to find missed messages.
    last_response_id: int | None = None  # type: ignore[assignment]


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
        self._bot: Any = commands.Bot(
            intents=discord.Intents.default(),
            command_prefix=">",
        )
        self._heartbeat: asyncio.Task | None = None
        self._ready_at: str | None = None

        # Register event handlers before the bot starts
        self._bot.event(self.on_ready)
        self._bot.event(self.on_message)

        # Missing-messages tracking: {channel_id_str: {"msg_id": int, "ts": str}}
        self._missing_msgs: dict[str, dict] = {}
        self._missing_path = _missing_msgs_file(_lc._kent_home())

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_missing_msgs(self) -> None:
        """Load previously tracked last-response IDs from disk."""
        try:
            data = json.loads(self._missing_path.read_text())
            if isinstance(data, dict):
                self._missing_msgs = data
                logger.info(f"[gateway] loaded {len(self._missing_msgs)} tracked channels")
        except Exception:
            self._missing_msgs = {}

    def _save_missing_msg(self, channel_id: int, msg_id: int) -> None:
        """Persist the last-response message ID for a channel."""
        self._missing_msgs[str(channel_id)] = {
            "msg_id": msg_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            tmp = self._missing_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._missing_msgs))
            tmp.replace(self._missing_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Post-ready gap scanner
    # ------------------------------------------------------------------

    async def _scan_and_respond(self) -> None:
        """On startup/restart, check tracked channels for unresponded messages.

        For each channel that was active when Kent went down, looks at Discord
        history between the *last successful response* and *now*. If there are
        any new human messages the gateway didn't reply to, processes them now.
        This catches messages lost due to disconnects, crashes, or misfires.
        """
        bot_user = self._bot.user
        if not bot_user or not self._missing_msgs:
            return

        scanned = 0
        acted = 0
        now_cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        for cid_str, info in list(self._missing_msgs.items()):
            cid = int(cid_str)
            last_id = int(info.get("msg_id", 0))
            ts_str = info.get("ts", "")

            # Skip entries older than ~12 hours (cleanup stale trackers)
            try:
                last_ts = datetime.fromisoformat(ts_str)
                if (datetime.now(timezone.utc) - last_ts).total_seconds() > 12 * 3600:
                    del self._missing_msgs[cid_str]
                    continue
            except (ValueError, TypeError):
                pass

            try:
                channel = await self._bot.fetch_channel(cid)
            except Exception:
                # Channel no longer accessible (deleted, kicked, etc.)
                del self._missing_msgs[cid_str]
                continue

            scanned += 1
            messages_to_process: list[Any] = []

            if last_id > 0:
                # Look for messages AFTER our last response
                try:
                    async for msg in channel.history(after=bot_user.created_at, limit=50):
                        if msg.id == last_id:
                            break
                        if msg.author.bot:
                            continue
                        # Check if Kent already responded (look for a message from us after this one)
                        found_reply = False
                        try:
                            async for prev in channel.history(before=msg.created_at, limit=1):
                                if prev and prev.author == bot_user:
                                    found_reply = True
                                    break
                        except Exception:
                            pass
                        if not found_reply:
                            messages_to_process.append(msg)
                except Exception:
                    pass
            else:
                # First time seeing this channel — grab recent unanswered messages
                try:
                    async for msg in channel.history(limit=50):
                        if msg.author.bot:
                            continue
                        # Has Kent replied?
                        found_reply = False
                        try:
                            async for prev in channel.history(before=msg.created_at, limit=1):
                                if prev and prev.author == bot_user:
                                    found_reply = True
                                    break
                        except Exception:
                            pass
                        if not found_reply:
                            messages_to_process.append(msg)
                except Exception:
                    pass

            if messages_to_process:
                acted += len(messages_to_process)
                logger.info(
                    f"[gateway] scanning #{channel.name} (cid={cid}): "
                    f"{len(messages_to_process)} unresponded message(s)"
                )
                # Process messages oldest-first
                for msg in sorted(messages_to_process, key=lambda m: m.created_at):
                    try:
                        await self._handle_turn(await self._session_for(msg), msg)
                    except Exception:
                        logger.exception("[gateway] failed to process missed message %s", msg.id)

        if acted > 0:
            print(
                f"[gateway] post-ready scan complete: scanned {scanned} channels, "
                f"replied to {acted} missed message(s)",
                flush=True,
            )
        else:
            print(f"[gateway] post-ready scan complete: {scanned} channels checked, nothing missed", flush=True)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

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

        # --- Load previously tracked channels and scan for missed messages ---
        self._load_missing_msgs()
        if self._missing_msgs:
            try:
                await self._scan_and_respond()
            except Exception:
                logger.exception("[gateway] post-ready scan failed")

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
        sent_any_text = False
        terminal_reason: str | None = None

        async def _send(text: str) -> bool:
            nonlocal sent_any_text
            ok = False
            for chunk in _split_for_discord(text):
                if not chunk:
                    continue
                try:
                    await message.channel.send(chunk)
                    sent_any_text = True
                    ok = True
                except Exception:
                    logger.exception("reply send failed")
                    return ok
            return ok

        try:
            async with message.channel.typing():
                async for ev in agent_run(
                    messages=history,
                    tools=session.registry,
                    llm=self._llm,
                    system=system_prompt,
                    max_turns=25,
                    memory_store=session.store,
                ):
                    if isinstance(ev, AssistantMessageComplete):
                        new_messages.append(ev.message.to_openai_dict())
                        text = (ev.message.content or "").strip()
                        if text:
                            await _send(text)
                    elif isinstance(ev, Terminal):
                        terminal_reason = ev.reason
                        break
        except Exception:
            logger.exception("agent run failed")
            try:
                await message.channel.send(
                    "[gateway] sorry — internal error processing that message."
                )
            except Exception:
                pass
            new_messages.append({"role": "assistant", "content": "[error]"})
            terminal_reason = "exception"

        # Persist: update session history, save last-response ID, track missed msgs
        session.history.extend(new_messages)
        self._save_missing_msg(int(message.channel.id), message.id)

        # Trim very long histories
        if len(session.history) > 200:
            session.history = session.history[-150:]

        self._write_status_snapshot()


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
