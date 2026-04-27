"""Unit tests for the SetWing tool."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_store(tmp_path: Path):
    """Return a minimal MemPalaceStore with isolated kent_home.

    None of the SetWing tests trigger record_turn / wake_up / recall, so we don't
    need to stub mempalace.sweeper. The MemPalaceStore constructor does not import
    mempalace — all mempalace imports are lazy inside individual methods.
    """
    pytest.importorskip("mempalace", reason="mempalace not installed")

    from agent.memory.mempalace_store import MemPalaceStore

    return MemPalaceStore(palace_path=tmp_path / "palace", kent_home=tmp_path)


async def _call_tool(tool, **kwargs):
    """Helper to invoke a tool's call() with a fake context."""
    ctx = MagicMock()
    ctx.signal = None
    args = tool.input_model(**kwargs)
    return await tool.call(args, ctx)


@pytest.mark.asyncio
async def test_switch_to_existing_wing(tmp_path):
    from agent.memory.wings import write_intent
    from agent.builtin.set_wing import SetWing

    write_intent("my-project", "test project", home=tmp_path)
    (tmp_path / "diaries" / "my-project").mkdir(parents=True, exist_ok=True)

    store = _make_store(tmp_path)
    tool = SetWing(store)

    result = await _call_tool(tool, name="my-project")
    assert not result.is_error
    assert "my-project" in result.output
    assert store.active_wing == "my-project"


@pytest.mark.asyncio
async def test_register_new_wing_with_intent(tmp_path):
    from agent.builtin.set_wing import SetWing
    from agent.memory.wings import read_intent

    store = _make_store(tmp_path)
    tool = SetWing(store)

    result = await _call_tool(tool, name="terraform-deploys", intent="monitors terraform")
    assert not result.is_error
    assert "terraform-deploys" in result.output
    assert store.active_wing == "terraform-deploys"
    assert read_intent("terraform-deploys", home=tmp_path) == "monitors terraform"


@pytest.mark.asyncio
async def test_missing_wing_without_intent_returns_error(tmp_path):
    from agent.builtin.set_wing import SetWing

    store = _make_store(tmp_path)
    tool = SetWing(store)

    result = await _call_tool(tool, name="newwing")
    assert result.is_error
    assert "intent" in result.output.lower()
    assert "newwing" in result.output


@pytest.mark.asyncio
async def test_invalid_wing_name_returns_error(tmp_path):
    from agent.builtin.set_wing import SetWing

    store = _make_store(tmp_path)
    tool = SetWing(store)

    result = await _call_tool(tool, name="Invalid Name!")
    assert result.is_error
    assert "invalid" in result.output.lower()


@pytest.mark.asyncio
async def test_uppercase_wing_name_gets_lowercased(tmp_path):
    from agent.builtin.set_wing import SetWing
    from agent.memory.wings import list_wings

    store = _make_store(tmp_path)
    tool = SetWing(store)

    result = await _call_tool(tool, name="MyProject", intent="a project")
    assert not result.is_error
    assert store.active_wing == "myproject"
    assert "myproject" in list_wings(home=tmp_path)


@pytest.mark.asyncio
async def test_switch_persists_active_wing(tmp_path):
    from agent.builtin.set_wing import SetWing
    from agent.memory.wings import read_active_wing

    store = _make_store(tmp_path)
    tool = SetWing(store)

    await _call_tool(tool, name="persisted", intent="should be saved")
    assert read_active_wing(home=tmp_path) == "persisted"


@pytest.mark.asyncio
async def test_switch_to_existing_shows_intent(tmp_path):
    from agent.memory.wings import write_intent
    from agent.builtin.set_wing import SetWing

    write_intent("myproj", "builds the thing", home=tmp_path)
    (tmp_path / "diaries" / "myproj").mkdir(parents=True, exist_ok=True)

    store = _make_store(tmp_path)
    tool = SetWing(store)

    result = await _call_tool(tool, name="myproj")
    assert "builds the thing" in result.output
