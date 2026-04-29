from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SwapPair:
    actor_base_url: str
    actor_api_key: str
    actor_model: str
    actor_family: str
    critic_base_url: str
    critic_api_key: str
    critic_model: str
    critic_family: str


@dataclass(frozen=True)
class ConsensusCritic:
    base_url: str
    api_key: str
    model: str
    family: str


class FamilyCollisionError(ValueError):
    pass


def _read_toml(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_pairs(config_path: Path | None = None) -> list[SwapPair]:
    """Load swap pairs from ~/.kent/swap_pairs.toml, rejecting same-family pairs."""
    path = config_path or Path.home() / ".kent" / "swap_pairs.toml"
    data = _read_toml(path)
    if data is None:
        return []

    actors = data.get("actors", [])
    critics = data.get("critics", [])
    pairs = []
    for actor in actors:
        for critic in critics:
            if actor["family"] == critic["family"]:
                raise FamilyCollisionError(
                    f"Same-family pair rejected: {actor['family']} — "
                    f"actor={actor['model']}, critic={critic['model']}"
                )
            pairs.append(SwapPair(
                actor_base_url=actor["base_url"],
                actor_api_key=actor.get("api_key", ""),
                actor_model=actor["model"],
                actor_family=actor["family"],
                critic_base_url=critic["base_url"],
                critic_api_key=critic.get("api_key", ""),
                critic_model=critic["model"],
                critic_family=critic["family"],
            ))
    return pairs


def load_consensus_critic(config_path: Path | None = None) -> ConsensusCritic | None:
    """Load the optional [consensus_critic] block from swap_pairs.toml.

    Used by Plan §"Cross-critic consensus check": a third critic from a
    different family that scores rollouts alongside the primary so we can
    detect rank-correlation drift over time.
    """
    path = config_path or Path.home() / ".kent" / "swap_pairs.toml"
    data = _read_toml(path)
    if not data:
        return None
    block = data.get("consensus_critic")
    if not block:
        return None
    return ConsensusCritic(
        base_url=block["base_url"],
        api_key=block.get("api_key", ""),
        model=block["model"],
        family=block["family"],
    )


def sweep_pairs(config_path: Path | None = None) -> Iterator[SwapPair]:
    """Yield all valid (actor x critic) cross-family pairs."""
    yield from load_pairs(config_path)
