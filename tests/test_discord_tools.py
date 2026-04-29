"""Offline tests for kent's Discord tools (mocked bot/channel)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("discord", reason="discord.py not installed")


def _make_ctx():
    ctx = MagicMock()
    ctx.signal = None
    ctx.expose_tool_errors = False
    return ctx


def _make_bot_with_channel():
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock()
    channel.create_thread = AsyncMock()
    channel.history = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.change_presence = AsyncMock()
    return bot, channel


# ---------- discord_send ----------------------------------------------------- #

@pytest.mark.asyncio
async def test_discord_send_happy_path():
    from agent.builtin.discord_send import DiscordSend

    bot, channel = _make_bot_with_channel()
    tool = DiscordSend(bot=bot, default_channel_id=42)
    result = await tool.call(tool.input_model(content="hello"), _make_ctx())

    assert not result.is_error
    channel.send.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_discord_send_uses_default_channel_when_arg_omitted():
    from agent.builtin.discord_send import DiscordSend

    bot, _ = _make_bot_with_channel()
    tool = DiscordSend(bot=bot, default_channel_id=42)
    await tool.call(tool.input_model(content="x"), _make_ctx())

    bot.get_channel.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_discord_send_chunks_long_content():
    from agent.builtin.discord_send import DiscordSend

    bot, channel = _make_bot_with_channel()
    tool = DiscordSend(bot=bot, default_channel_id=42)

    # 5000 chars of words → ≥ 3 chunks at the 1900 limit
    long = ("word " * 1100).strip()
    result = await tool.call(tool.input_model(content=long), _make_ctx())

    assert not result.is_error
    assert channel.send.await_count >= 3


@pytest.mark.asyncio
async def test_discord_send_error_path():
    from agent.builtin.discord_send import DiscordSend

    bot, channel = _make_bot_with_channel()
    channel.send.side_effect = RuntimeError("boom")
    tool = DiscordSend(bot=bot, default_channel_id=42)
    result = await tool.call(tool.input_model(content="x"), _make_ctx())

    assert result.is_error
    assert "boom" in result.output


# ---------- discord_react ---------------------------------------------------- #

@pytest.mark.asyncio
async def test_discord_react_happy_path():
    from agent.builtin.discord_react import DiscordReact

    bot, channel = _make_bot_with_channel()
    msg = MagicMock()
    msg.add_reaction = AsyncMock()
    channel.fetch_message.return_value = msg

    tool = DiscordReact(bot=bot, default_channel_id=42)
    result = await tool.call(
        tool.input_model(message_id=99, emoji="🔥"), _make_ctx()
    )

    assert not result.is_error
    msg.add_reaction.assert_awaited_once_with("🔥")


@pytest.mark.asyncio
async def test_discord_react_error_path():
    from agent.builtin.discord_react import DiscordReact

    bot, channel = _make_bot_with_channel()
    channel.fetch_message.side_effect = RuntimeError("nope")
    tool = DiscordReact(bot=bot, default_channel_id=42)
    result = await tool.call(
        tool.input_model(message_id=99, emoji="🔥"), _make_ctx()
    )

    assert result.is_error
    assert "nope" in result.output


# ---------- discord_thread_create -------------------------------------------- #

@pytest.mark.asyncio
async def test_discord_thread_create_happy_path():
    from agent.builtin.discord_thread_create import DiscordThreadCreate

    bot, channel = _make_bot_with_channel()
    fake_thread = MagicMock()
    fake_thread.id = 7777
    channel.create_thread.return_value = fake_thread

    tool = DiscordThreadCreate(bot=bot, default_channel_id=42)
    result = await tool.call(tool.input_model(name="side topic"), _make_ctx())

    assert not result.is_error
    channel.create_thread.assert_awaited_once()
    assert "7777" in result.output


@pytest.mark.asyncio
async def test_discord_thread_create_error_path():
    from agent.builtin.discord_thread_create import DiscordThreadCreate

    bot, channel = _make_bot_with_channel()
    channel.create_thread.side_effect = RuntimeError("oops")
    tool = DiscordThreadCreate(bot=bot, default_channel_id=42)
    result = await tool.call(tool.input_model(name="x"), _make_ctx())

    assert result.is_error
    assert "oops" in result.output


# ---------- discord_set_status ----------------------------------------------- #

@pytest.mark.asyncio
async def test_discord_set_status_happy_path():
    from agent.builtin.discord_set_status import DiscordSetStatus

    bot, _ = _make_bot_with_channel()
    tool = DiscordSetStatus(bot=bot)
    result = await tool.call(
        tool.input_model(status="dnd", activity="thinking"), _make_ctx()
    )

    assert not result.is_error
    bot.change_presence.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_set_status_error_path():
    from agent.builtin.discord_set_status import DiscordSetStatus

    bot, _ = _make_bot_with_channel()
    bot.change_presence.side_effect = RuntimeError("api")
    tool = DiscordSetStatus(bot=bot)
    result = await tool.call(
        tool.input_model(status="online"), _make_ctx()
    )

    assert result.is_error
    assert "api" in result.output


# ---------- discord_read_history --------------------------------------------- #


class _FakeMsg:
    def __init__(self, mid: int, name: str, content: str):
        self.id = mid
        self.author = MagicMock()
        self.author.display_name = name
        self.author.name = name
        self.content = content


@pytest.mark.asyncio
async def test_discord_read_history_passes_oldest_first_true():
    from agent.builtin.discord_read_history import DiscordReadHistory

    bot, channel = _make_bot_with_channel()
    captured: dict = {}

    def history_call(**kwargs):
        captured.update(kwargs)
        async def _empty():
            for _ in []:
                yield None
        return _empty()

    channel.history = history_call
    tool = DiscordReadHistory(bot=bot, default_channel_id=42)
    result = await tool.call(tool.input_model(limit=5), _make_ctx())

    assert not result.is_error
    assert captured.get("oldest_first") is True
    assert captured.get("limit") == 5


@pytest.mark.asyncio
async def test_discord_read_history_returns_chronological_lines():
    from agent.builtin.discord_read_history import DiscordReadHistory

    bot, channel = _make_bot_with_channel()
    msgs = [_FakeMsg(1, "alice", "hi"), _FakeMsg(2, "bob", "hello")]

    def history_call(**kwargs):
        async def _gen():
            for m in msgs:
                yield m
        return _gen()

    channel.history = history_call

    tool = DiscordReadHistory(bot=bot, default_channel_id=42)
    result = await tool.call(tool.input_model(), _make_ctx())

    assert not result.is_error
    assert isinstance(result.output, dict)
    lines = result.output["messages"]
    assert any("alice" in line for line in lines)
    assert any("bob" in line for line in lines)
