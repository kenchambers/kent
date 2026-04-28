"""Tests for swap pair loading and family collision guard."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.training.swap_pair import load_pairs, sweep_pairs, FamilyCollisionError


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content)


def test_load_pairs_cross_family(tmp_path):
    config = tmp_path / "swap_pairs.toml"
    _write_toml(config, """
[[actors]]
base_url = "http://localhost:11434"
api_key = ""
model = "llama3"
family = "meta"

[[critics]]
base_url = "https://api.openai.com/v1"
api_key = "sk-test"
model = "gpt-4o"
family = "openai"
""")
    pairs = load_pairs(config)
    assert len(pairs) == 1
    assert pairs[0].actor_family == "meta"
    assert pairs[0].critic_family == "openai"


def test_same_family_raises(tmp_path):
    config = tmp_path / "swap_pairs.toml"
    _write_toml(config, """
[[actors]]
base_url = "http://a"
api_key = ""
model = "gpt-4o-mini"
family = "openai"

[[critics]]
base_url = "http://b"
api_key = ""
model = "gpt-4o"
family = "openai"
""")
    with pytest.raises(FamilyCollisionError):
        load_pairs(config)


def test_load_pairs_missing_file(tmp_path):
    pairs = load_pairs(tmp_path / "nonexistent.toml")
    assert pairs == []


def test_sweep_pairs_yields_all(tmp_path):
    config = tmp_path / "swap_pairs.toml"
    _write_toml(config, """
[[actors]]
base_url = "http://a1"
api_key = ""
model = "actor1"
family = "family_a"

[[actors]]
base_url = "http://a2"
api_key = ""
model = "actor2"
family = "family_b"

[[critics]]
base_url = "http://c1"
api_key = ""
model = "critic1"
family = "family_c"
""")
    pairs = list(sweep_pairs(config))
    assert len(pairs) == 2
    actor_models = {p.actor_model for p in pairs}
    assert actor_models == {"actor1", "actor2"}


def test_multi_actor_multi_critic_cross_product(tmp_path):
    config = tmp_path / "swap_pairs.toml"
    _write_toml(config, """
[[actors]]
base_url = "http://a"
api_key = ""
model = "a1"
family = "fa"

[[actors]]
base_url = "http://a"
api_key = ""
model = "a2"
family = "fb"

[[critics]]
base_url = "http://c"
api_key = ""
model = "c1"
family = "fc"

[[critics]]
base_url = "http://c"
api_key = ""
model = "c2"
family = "fd"
""")
    pairs = list(sweep_pairs(config))
    assert len(pairs) == 4
