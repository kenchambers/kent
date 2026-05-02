"""Phase A: verify record_turn tags new drawers with wing + room metadata."""
from __future__ import annotations

import pytest


@pytest.mark.memory
def test_record_turn_tags_wing_and_room(tmp_path):
    """New drawers written by record_turn carry wing and room metadata."""
    try:
        from mempalace.palace import get_collection
    except ImportError:
        pytest.skip("mempalace not installed")

    from agent.memory.mempalace_store import MemPalaceStore

    kent_home = tmp_path / "kent"
    kent_home.mkdir()
    (kent_home / "active_wing.txt").write_text("myproj\n")

    palace_path = kent_home / "palace"
    store = MemPalaceStore(palace_path=palace_path, kent_home=kent_home)
    assert store.active_wing == "myproj"

    session_id = store.session_id
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    store.record_turn(messages, session_id=session_id)

    col = get_collection(str(palace_path), create=False)
    # Read all drawers belonging to this session
    all_data = col.get(include=["metadatas"])
    ids = all_data.get("ids") or []
    metas = all_data.get("metadatas") or []

    tagged = [(did, m) for did, m in zip(ids, metas) if (m or {}).get("wing")]
    assert tagged, "expected at least one drawer to be tagged with wing metadata"

    for did, meta in tagged:
        assert meta.get("wing") == "myproj", f"drawer {did} has wrong wing: {meta}"
        assert meta.get("room") == "conversation", f"drawer {did} has wrong room: {meta}"


@pytest.mark.memory
def test_record_turn_tags_custom_room(tmp_path):
    """record_turn respects an explicit room kwarg."""
    try:
        from mempalace.palace import get_collection
    except ImportError:
        pytest.skip("mempalace not installed")

    from agent.memory.mempalace_store import MemPalaceStore

    kent_home = tmp_path / "kent"
    kent_home.mkdir()
    (kent_home / "active_wing.txt").write_text("projB\n")

    store = MemPalaceStore(palace_path=kent_home / "palace", kent_home=kent_home)
    messages = [{"role": "user", "content": "code question"}]
    store.record_turn(messages, session_id=store.session_id, room="code")

    col = get_collection(str(store.palace_path), create=False)
    all_data = col.get(include=["metadatas"])
    metas = all_data.get("metadatas") or []
    tagged = [m for m in metas if (m or {}).get("room") == "code"]
    assert tagged, "expected a drawer with room='code'"
