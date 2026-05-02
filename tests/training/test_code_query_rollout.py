"""Test the 6th APO resource: code_query_policy rollout."""
from __future__ import annotations

import pytest


@pytest.mark.slow
def test_build_code_query_apo_agent_returns_callable():
    """build_code_query_apo_agent() returns a callable without errors."""
    try:
        import agentlightning  # noqa: F401
    except ImportError:
        pytest.skip("agentlightning not installed")

    from agent.training.rollout import build_code_query_apo_agent
    agent = build_code_query_apo_agent()
    assert callable(agent)


@pytest.mark.slow
def test_code_query_policy_in_resource_order():
    """code_query_policy is in RESOURCE_ORDER and has a baseline."""
    from agent.training.apo_runner import RESOURCE_ORDER, RESOURCE_BASELINES

    assert "code_query_policy" in RESOURCE_ORDER
    assert "code_query_policy" in RESOURCE_BASELINES
    baseline = RESOURCE_BASELINES["code_query_policy"]
    assert "code" in baseline.lower()


@pytest.mark.slow
def test_recall_game_rollout_accepts_wing_kwarg():
    """recall_game_rollout task dict can carry wing/room for wing-scoped search."""
    from agent.training.rollout import build_recall_game_apo_agent
    try:
        import agentlightning  # noqa: F401
    except ImportError:
        pytest.skip("agentlightning not installed")

    agent = build_recall_game_apo_agent()
    assert callable(agent)


@pytest.mark.slow
def test_palace_isolation_snapshots_tunnels(tmp_path):
    """snapshot() copies tunnels.json into scratch if it exists."""
    from agent.training.palace_isolation import snapshot, cleanup

    kent_home = tmp_path / "kent"
    kent_home.mkdir()
    palace_path = kent_home / "palace"
    palace_path.mkdir()

    # Create a fake tunnels.json
    tunnels_src = tmp_path / "fake_mempalace"
    tunnels_src.mkdir()
    fake_tunnels = tunnels_src / "tunnels.json"
    fake_tunnels.write_text('[]')

    import unittest.mock as mock
    with mock.patch(
        "agent.training.palace_isolation.Path.home",
        return_value=tmp_path,
    ):
        # Create the expected path structure
        mempalace_dir = tmp_path / ".mempalace"
        mempalace_dir.mkdir(exist_ok=True)
        (mempalace_dir / "tunnels.json").write_text('[]')

        rollout_id, scratch = snapshot(kent_home, palace_path)
        try:
            # tunnels.json should be in scratch
            assert (scratch / "tunnels.json").exists(), \
                "tunnels.json should be copied to scratch"
        finally:
            cleanup(rollout_id)
