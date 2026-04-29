"""Tests for agent/gateway/heartbeat.py"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.gateway.heartbeat import (
    Heartbeat,
    default_heartbeat_md_text,
    parse_interval,
    read_heartbeat_md,
)


# ---------- parse_interval ------------------------------------------------- #

@pytest.mark.parametrize(
    "s, expected",
    [
        ("30s", 30.0),
        ("5m", 300.0),
        ("30m", 1800.0),
        ("1h", 3600.0),
        ("2h", 7200.0),
        ("0.5m", 30.0),
        ("90", 90.0),
        # off-tokens
        ("", None),
        ("off", None),
        ("OFF", None),
        ("disabled", None),
        ("0", None),
        ("none", None),
        ("  off  ", None),
        # invalid
        ("banana", None),
        ("-5m", None),
    ],
)
def test_parse_interval(s: str, expected: float | None) -> None:
    result = parse_interval(s)
    assert result == expected


def test_parse_interval_zero_is_off() -> None:
    assert parse_interval("0") is None


# ---------- read_heartbeat_md ---------------------------------------------- #

def test_read_heartbeat_md_missing(tmp_path: Path) -> None:
    result = read_heartbeat_md(tmp_path / "nope.md")
    assert result == ""


def test_read_heartbeat_md_present(tmp_path: Path) -> None:
    p = tmp_path / "HEARTBEAT.md"
    p.write_text("do something cool")
    assert read_heartbeat_md(p) == "do something cool"


def test_default_heartbeat_md_text_nonempty() -> None:
    text = default_heartbeat_md_text()
    assert len(text) > 50
    assert "discord_send" in text or "diary_write" in text or "tick" in text.lower()


# ---------- Heartbeat loop ------------------------------------------------- #

@pytest.fixture()
def fake_gateway():
    gw = MagicMock()
    gw._run_heartbeat_turn = AsyncMock()
    return gw


@pytest.mark.asyncio
async def test_heartbeat_loop_calls_tick(tmp_path: Path, fake_gateway):
    md = tmp_path / "HEARTBEAT.md"
    md.write_text("say hello")

    sleep_calls: list[float] = []

    async def fake_sleep(s: float):
        sleep_calls.append(s)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    hb = Heartbeat(
        gateway=fake_gateway,
        interval_s=60.0,
        channel_id=123,
        md_path=md,
    )

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with patch("agent.gateway.heartbeat._lc") as mock_lc:
            mock_lc.read_status.return_value = {}
            mock_lc.write_status = MagicMock()
            with pytest.raises(asyncio.CancelledError):
                await hb._loop()

    # _run_heartbeat_turn called once per sleep
    assert fake_gateway._run_heartbeat_turn.await_count >= 1
    fake_gateway._run_heartbeat_turn.assert_called_with(123, "say hello")


@pytest.mark.asyncio
async def test_heartbeat_loop_skips_empty_md(tmp_path: Path, fake_gateway):
    md = tmp_path / "HEARTBEAT.md"
    # File does not exist → read_heartbeat_md returns ""

    sleep_calls: list[float] = []

    async def fake_sleep(s: float):
        sleep_calls.append(s)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    hb = Heartbeat(
        gateway=fake_gateway,
        interval_s=60.0,
        channel_id=123,
        md_path=md,
    )

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await hb._loop()

    fake_gateway._run_heartbeat_turn.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_loop_survives_exception(tmp_path: Path, fake_gateway):
    """A tick exception must not break subsequent ticks."""
    md = tmp_path / "HEARTBEAT.md"
    md.write_text("do stuff")

    call_count = 0
    original_run = fake_gateway._run_heartbeat_turn

    async def boom(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("oops")

    fake_gateway._run_heartbeat_turn = boom

    sleep_calls: list[float] = []

    async def fake_sleep(s: float):
        sleep_calls.append(s)
        if len(sleep_calls) >= 3:
            raise asyncio.CancelledError()

    hb = Heartbeat(
        gateway=fake_gateway,
        interval_s=60.0,
        channel_id=999,
        md_path=md,
    )

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with patch("agent.gateway.heartbeat._lc") as mock_lc:
            mock_lc.read_status.return_value = {}
            mock_lc.write_status = MagicMock()
            with pytest.raises(asyncio.CancelledError):
                await hb._loop()

    # Should have been called twice (first raised, second succeeded)
    assert call_count >= 2
