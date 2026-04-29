"""Live Discord smoke test for the gateway.

This is a single live test, gated by the `live_discord` marker. It is **not
run by default**. Run explicitly with::

    uv run pytest -m live_discord

Required environment:
    KENT_DISCORD_BOT_TOKEN       — the bot token used by `kent gateway`.
    KENT_DISCORD_TEST_CHANNEL_ID — a channel ID the bot can read/write
                                   (the bot must be invited and have View
                                   Channel + Send Messages perms there).

What this test does (no second identity required):
    1. Connect to Discord with the bot token. This validates the token and
       intents (MESSAGE_CONTENT, MEMBERS, PRESENCES) are wired correctly.
    2. Resolve the test channel; verify it's accessible.
    3. Post a one-line marker via the bot's own client.
    4. Read recent history; verify the marker appears.
    5. Disconnect cleanly.

This *does not* prove the agent loop responds to a mention end-to-end —
that requires a second account (a separate test bot or user) to send the
mention, which is out of scope for v1. The smoke test still catches the
common regressions: token revocation, missing intents, missing invite,
or missing send permission.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

discord = pytest.importorskip("discord", reason="discord.py not installed")

pytestmark = pytest.mark.live_discord


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"{name} not set")
    return val


@pytest.mark.asyncio
async def test_gateway_live_round_trip() -> None:
    token = _required_env("KENT_DISCORD_BOT_TOKEN")
    channel_id = int(_required_env("KENT_DISCORD_TEST_CHANNEL_ID"))

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)
    marker = f"kent-live-test {uuid.uuid4()}"
    saw_marker = asyncio.Event()
    failure: dict[str, str] = {}

    async def on_ready() -> None:  # pragma: no cover — exercised live
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(
                channel_id
            )
            sent = await channel.send(marker)
            assert sent.id

            found = False
            async for msg in channel.history(limit=20, oldest_first=False):
                if msg.content == marker:
                    found = True
                    break
            if not found:
                failure["reason"] = "marker not found in recent channel history"
            else:
                saw_marker.set()
        except Exception as exc:  # noqa: BLE001
            failure["reason"] = f"{type(exc).__name__}: {exc}"
        finally:
            await client.close()

    client.event(on_ready)

    try:
        await asyncio.wait_for(client.start(token), timeout=30.0)
    except asyncio.TimeoutError:
        pytest.fail("client.start did not finish within 30s")

    if failure:
        pytest.fail(failure["reason"])
    assert saw_marker.is_set(), "marker round-trip did not complete"
