"""Offline tests for the `kent gateway` CLI: argparse, chunking, token resolution."""
from __future__ import annotations


def test_parser_gateway_default_action_is_start():
    from agent.cli import _build_parser

    args = _build_parser().parse_args(["gateway"])
    assert args.command == "gateway"
    assert args.action == "start"


def test_parser_gateway_run_subaction():
    from agent.cli import _build_parser

    args = _build_parser().parse_args(
        ["gateway", "run", "--mention-only", "--status", "online"]
    )
    assert args.action == "run"
    assert args.mention_only is True
    assert args.status == "online"


def test_parser_gateway_status_subaction():
    from agent.cli import _build_parser, cmd_gateway

    args = _build_parser().parse_args(["gateway", "status"])
    assert args.action == "status"
    assert args.func is cmd_gateway


def test_parser_gateway_config_subaction():
    from agent.cli import _build_parser

    args = _build_parser().parse_args(["gateway", "config", "--token"])
    assert args.action == "config"
    assert args.token is True


def test_split_for_discord_chunks_long_text():
    from agent.gateway.discord_bot import _split_for_discord

    text = ("word " * 1000).strip()  # well over 1900 chars
    chunks = _split_for_discord(text, limit=1900)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 1900
        # No mid-word splits — chunks should not start or end with partial token
        assert not chunk.startswith("ord")
        assert not chunk.endswith("wor")


def test_split_for_discord_preserves_short_text():
    from agent.gateway.discord_bot import _split_for_discord

    short = "hello world"
    assert _split_for_discord(short) == [short]


def test_resolve_discord_token_prefers_env(monkeypatch):
    from agent import cli

    monkeypatch.setenv("KENT_DISCORD_BOT_TOKEN", "from-env")
    monkeypatch.setattr(cli, "load_credentials", lambda: {})
    assert cli._resolve_discord_token() == "from-env"


def test_resolve_discord_token_falls_back_to_credentials(monkeypatch):
    from agent import cli

    monkeypatch.delenv("KENT_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "load_credentials", lambda: {"discord_bot_token": "from-creds"}
    )
    assert cli._resolve_discord_token() == "from-creds"


def test_resolve_discord_token_returns_none_when_unset(monkeypatch):
    from agent import cli

    monkeypatch.delenv("KENT_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_credentials", lambda: {})
    assert cli._resolve_discord_token() is None


def test_wing_for_channel_guild_format():
    from agent.gateway.discord_bot import _wing_for_channel
    from unittest.mock import MagicMock

    msg = MagicMock()
    msg.guild.id = 123456789012345678
    msg.channel.id = 987654321098765432
    wing = _wing_for_channel(msg)
    assert wing == "discord_123456789012345678_987654321098765432"
    assert len(wing) <= 64

    # Validate against the wings sanitizer regex
    from agent.memory.wings import sanitize_wing
    assert sanitize_wing(wing) == wing


def test_wing_for_channel_dm_format():
    from agent.gateway.discord_bot import _wing_for_channel
    from agent.memory.wings import sanitize_wing
    from unittest.mock import MagicMock

    msg = MagicMock()
    msg.guild = None
    msg.author.id = 111111111111111111
    wing = _wing_for_channel(msg)
    assert wing == "discord_dm_111111111111111111"
    assert len(wing) <= 64
    assert sanitize_wing(wing) == wing


def test_parser_gateway_run_accepts_overrides():
    """--service / --model / --wing parse and reach args namespace."""
    from agent.cli import _build_parser, SUPPORTED_SERVICES

    svc = next(iter(SUPPORTED_SERVICES))
    args = _build_parser().parse_args(
        [
            "gateway",
            "run",
            "--service",
            svc,
            "--model",
            "test-model-id",
            "--wing",
            "discord_test",
        ]
    )
    assert args.action == "run"
    assert args.service == svc
    assert args.model == "test-model-id"
    assert args.wing == "discord_test"


def test_build_run_argv_forwards_new_overrides():
    """`start` must propagate --service/--model/--wing to the detached `run`."""
    from agent.cli import _build_parser, _build_run_argv, SUPPORTED_SERVICES

    svc = next(iter(SUPPORTED_SERVICES))
    args = _build_parser().parse_args(
        [
            "gateway",
            "start",
            "--all-messages",
            "--service",
            svc,
            "--model",
            "test-model",
            "--wing",
            "discord_global",
        ]
    )
    argv = _build_run_argv(args)
    assert "--all-messages" in argv
    assert argv[argv.index("--service") + 1] == svc
    assert argv[argv.index("--model") + 1] == "test-model"
    assert argv[argv.index("--wing") + 1] == "discord_global"


def test_format_uptime_units():
    from agent.cli import _format_uptime

    assert _format_uptime(5) == "5s"
    assert _format_uptime(125) == "2m05s"
    assert _format_uptime(3725) == "1h02m"
    assert _format_uptime(90061) == "1d01h"


def test_gateway_status_uses_status_json(monkeypatch, tmp_path, capsys):
    """cmd_gateway_status reports channels + ready_at from gateway.status.json."""
    import argparse
    import os

    monkeypatch.setenv("KENT_HOME", str(tmp_path))

    from agent.gateway import lifecycle as lc

    own_pid = os.getpid()
    lc.write_pid(own_pid)
    lc.write_status(
        {
            "ready_at": "2026-04-29T10:00:00+00:00",
            "user": "kent#1234",
            "user_id": 42,
            "channels": 3,
            "mention_only": True,
            "status": "online",
        }
    )

    from agent.cli import cmd_gateway_status

    rc = cmd_gateway_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "channels: 3" in out
    assert "kent#1234" in out
    assert "ready at: 2026-04-29T10:00:00+00:00" in out
    assert "uptime  :" in out
