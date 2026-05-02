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
from ..events import AssistantMessageComplete, ModelError, Terminal
from ..llm import OpenAICompatibleLLM
from ..loop import run as agent_run
from ..orchestration import INBOX, REGISTRY
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
    heartbeat_use_dm: bool = False          # DM the bot owner when no channel_id set


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
    # Per-channel notifier task — surfaces background <task-notification>s to the
    # channel between user turns. Started lazily in _session_for_channel_id.
    notifier: asyncio.Task | None = None


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


def _safe_history_window(history: list[dict], max_keep: int) -> list[dict]:
    """Trim history to the last max_keep messages, but realign the start to a
    `user` turn boundary so we never send orphan tool messages whose matching
    assistant tool_calls were dropped — OpenAI-compatible servers reject those
    with a 400.
    """
    if len(history) <= max_keep:
        return history
    window = history[-max_keep:]
    for i, msg in enumerate(window):
        if msg.get("role") == "user":
            return window[i:]
    return []


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
    system_prompt: str | None = None,
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
    from ..builtin.shell_spawn import ShellSpawn
    from ..builtin.spawn import Spawn
    from ..builtin.task_status import TaskStatus
    from ..builtin.task_stop import TaskStop
    from ..builtin.tunnel_create import TunnelCreate
    from ..builtin.web_fetch import WebFetch
    from ..builtin.web_search import WebSearch

    registry = ToolRegistry()
    registry.register(WebSearch())
    registry.register(WebFetch())
    registry.register(Shell())
    registry.register(ShellSpawn())
    registry.register(TaskStatus())
    registry.register(TaskStop())
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

    # Spawn registered last so it can hand the *full* registry (including Discord
    # tools and itself) to each subagent for recursive delegation.
    registry.register(Spawn(
        parent_registry=registry, llm=llm, memory_store=memory_store,
        system_prompt=system_prompt,
    ))
    return registry


def _channel_session_id(channel_id: int) -> str:
    return f"discord:{channel_id}"


def _extract_xml_field(content: str, tag: str) -> str:
    open_tag = f"<{tag}>"
    close_tag = f"</{tag}>"
    i = content.find(open_tag)
    if i < 0:
        return ""
    j = content.find(close_tag, i + len(open_tag))
    if j < 0:
        return ""
    return content[i + len(open_tag): j]


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
        self._llm = llm_factory()
        self._system_prompt_factory = system_prompt_factory
        self._store_factory = store_factory or self._default_store_factory
        self._wing_override = wing_override
        self._sessions: dict[int, ChannelSession] = {}
        self._sessions_lock = asyncio.Lock()
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        self._bot: Any = commands.Bot(
            command_prefix=">",
            intents=intents,
        )
        self._heartbeat: asyncio.Task | None = None
        self._ready_at: str | None = None

        # Register event handlers before the bot starts
        self._bot.event(self.on_ready)
        self._bot.event(self.on_message)

        # Missing-messages tracking: {channel_id_str: {"msg_id": int, "ts": str}}
        self._missing_msgs: dict[str, dict] = {}
        self._missing_path = _missing_msgs_file(_lc._kent_home())

    @staticmethod
    def _default_store_factory() -> "MemPalaceStore":
        from ..memory.mempalace_store import MemPalaceStore
        return MemPalaceStore()

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
                channel_label = getattr(channel, "name", None)
                if not channel_label:
                    recipient = getattr(channel, "recipient", None)
                    channel_label = f"DM:{getattr(recipient, 'name', None) or cid}"
                logger.info(
                    f"[gateway] scanning #{channel_label} (cid={cid}): "
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

        await self._start_heartbeat()

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
            sys_prompt = self._system_prompt_factory(store) + "\n\n" + DISCORD_SUFFIX
            registry = _build_discord_registry(
                self._bot, cid, llm=self._llm, memory_store=store,
                system_prompt=sys_prompt,
            )
            sess = ChannelSession(wing=wing, store=store, registry=registry)
            sess.notifier = asyncio.create_task(
                self._channel_notifier(cid, _channel_session_id(cid))
            )
            self._sessions[cid] = sess
            self._write_status_snapshot()
            return sess

    async def _channel_notifier(self, channel_id: int, session_id: str) -> None:
        """Per-channel proactive notifier.

        Surfaces task completions to the channel between user turns. Peeks the
        inbox without consuming so the next user turn's drain still feeds the
        notifications back into the LLM as user-role messages.
        """
        seen: set[int] = set()
        while True:
            try:
                await INBOX.wait_for_any(session_id)
            except asyncio.CancelledError:
                return
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except Exception:
                # Channel went away — stop notifying.
                return
            for msg in INBOX.peek(session_id):
                mid = id(msg)
                if mid in seen:
                    continue
                seen.add(mid)
                content = msg.get("content", "")
                tid = _extract_xml_field(content, "task-id")
                status = _extract_xml_field(content, "status")
                summary = _extract_xml_field(content, "summary")
                line = f"task `{tid}` {status}"
                if summary:
                    line += f" — {summary[:80]}"
                if line.strip():
                    try:
                        await channel.send(line)
                    except Exception:
                        logger.exception("notifier: send failed for channel %d", channel_id)
            await asyncio.sleep(0)


    async def _session_for(self, message: Any) -> ChannelSession:
        cid = int(message.channel.id)
        wing_name = _wing_for_channel(message)
        return await self._session_for_channel_id(cid, wing_name=wing_name)

    def _should_respond(self, message: Any) -> bool:
        if message.author.id == self._bot.user.id:
            return False
        if getattr(message.author, "bot", False):
            return False
        # DMs are inherently directed at the bot — always respond.
        if message.guild is None:
            return True
        if self._settings.mention_only:
            return self._bot.user in message.mentions
        return True

    async def on_message(self, message: Any) -> None:
        try:
            if self._bot.user is None:
                return
            is_dm = message.guild is None
            print(
                f"[on_message] from={message.author} dm={is_dm} "
                f"content={message.content!r:.60}",
                flush=True,
            )
            if not self._should_respond(message):
                print(f"[on_message] skipped (mention_only={self._settings.mention_only})", flush=True)
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

        # Drain pending background-task notifications BEFORE the new user turn.
        cid = int(message.channel.id)
        session_id = _channel_session_id(cid)
        pending_msgs, drained_ids = INBOX.drain(session_id)
        pending_msgs = [m for m in pending_msgs if (m.get("content") or "").strip()]
        for tid in drained_ids:
            REGISTRY.drop(tid)

        history = [*session.history, *pending_msgs, {"role": "user", "content": user_text}]
        new_messages: list[dict] = []
        new_messages.extend(pending_msgs)
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
                    parent_session_id=session_id,
                    depth=0,
                ):
                    if isinstance(ev, AssistantMessageComplete):
                        new_messages.append(ev.message.to_openai_dict())
                        text = (ev.message.content or "").strip()
                        if text:
                            await _send(text)
                    elif isinstance(ev, ModelError):
                        logger.error("model error: %s: %s", type(ev.error).__name__, ev.error)
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

        if terminal_reason and terminal_reason != "completed" and not sent_any_text:
            try:
                await message.channel.send(
                    f"[gateway] run terminated ({terminal_reason}) without producing a "
                    "final reply. Reply to continue or rephrase."
                )
            except Exception:
                logger.exception("fallback send failed")

        # Persist: update session history, save last-response ID, track missed msgs
        session.history.extend(new_messages)
        self._save_missing_msg(int(message.channel.id), message.id)

        # Trim very long histories (safe-realign to a user-turn boundary).
        if len(session.history) > 200:
            session.history = _safe_history_window(session.history, 150)

        self._write_status_snapshot()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _start_heartbeat(self) -> None:
        from .heartbeat import Heartbeat, parse_interval

        interval_str = self._settings.heartbeat_interval
        if not interval_str:
            return
        interval_s = parse_interval(interval_str)
        if interval_s is None:
            return

        channel_id = self._settings.heartbeat_channel_id

        if channel_id is None:
            if not self._settings.heartbeat_use_dm:
                logger.info("heartbeat: no channel configured — skipping")
                return
            try:
                app_info = await self._bot.application_info()
                owner = app_info.owner
                dm_channel = await owner.create_dm()
                channel_id = dm_channel.id
                logger.info(
                    "heartbeat: resolved DM channel %d for owner %s",
                    channel_id, owner,
                )
            except Exception:
                logger.exception("heartbeat: failed to resolve bot owner DM")
                return

        hb = Heartbeat(gateway=self, interval_s=interval_s, channel_id=channel_id)
        self._heartbeat = hb.start()
        logger.info(
            "heartbeat: started (interval=%ds, channel=%d)", interval_s, channel_id
        )

    async def _run_heartbeat_turn(self, channel_id: int, prompt_text: str) -> None:
        try:
            channel = await self._bot.fetch_channel(channel_id)
        except Exception:
            logger.exception("heartbeat: failed to fetch channel %d", channel_id)
            return

        session = await self._session_for_channel_id(
            channel_id, wing_name=f"discord_hb_{channel_id}"
        )
        assert session.store is not None and session.registry is not None

        session_id = _channel_session_id(channel_id)
        pending_msgs, drained_ids = INBOX.drain(session_id)
        pending_msgs = [m for m in pending_msgs if (m.get("content") or "").strip()]
        for tid in drained_ids:
            REGISTRY.drop(tid)

        system_prompt = self._system_prompt_factory(session.store) + "\n\n" + DISCORD_SUFFIX
        history = [*session.history, *pending_msgs, {"role": "user", "content": prompt_text}]
        new_messages: list[dict] = list(pending_msgs)

        async with session.lock:
            try:
                async with channel.typing():
                    async for ev in agent_run(
                        messages=history,
                        tools=session.registry,
                        llm=self._llm,
                        system=system_prompt,
                        max_turns=10,
                        memory_store=session.store,
                        parent_session_id=session_id,
                        depth=0,
                    ):
                        if isinstance(ev, AssistantMessageComplete):
                            new_messages.append(ev.message.to_openai_dict())
                            text = (ev.message.content or "").strip()
                            if text:
                                for chunk in _split_for_discord(text):
                                    if chunk:
                                        try:
                                            await channel.send(chunk)
                                        except Exception:
                                            logger.exception("heartbeat: send failed")
                        elif isinstance(ev, Terminal):
                            break
            except Exception:
                logger.exception("heartbeat turn raised (channel=%d)", channel_id)
            finally:
                session.history.extend(new_messages)
                if len(session.history) > 200:
                    session.history = _safe_history_window(session.history, 150)

    async def start(self) -> None:
        await self._bot.start(self._token)

    async def close(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):
                pass
        for sess in list(self._sessions.values()):
            if sess.notifier is not None and not sess.notifier.done():
                sess.notifier.cancel()
                try:
                    await sess.notifier
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
