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


class FamilyCollisionError(ValueError):
    pass


def load_pairs(config_path: Path | None = None) -> list[SwapPair]:
    """Load swap pairs from ~/.kent/swap_pairs.toml, rejecting same-family pairs."""
    path = config_path or Path.home() / ".kent" / "swap_pairs.toml"
    if not path.exists():
        return []
    with open(path, "rb") as f:
        data = tomllib.load(f)

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


def sweep_pairs(config_path: Path | None = None) -> Iterator[SwapPair]:
    """Yield all valid (actor x critic) cross-family pairs."""
    yield from load_pairs(config_path)
