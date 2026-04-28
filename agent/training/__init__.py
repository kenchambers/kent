from dataclasses import dataclass, field
from pathlib import Path

ROLLOUT_BASE = Path.home() / ".mempalace" / "_rollouts"


@dataclass
class TrainingConfig:
    actor_base_url: str
    actor_api_key: str
    actor_model: str
    critic_base_url: str
    critic_api_key: str
    critic_model: str
    actor_family: str
    critic_family: str
    palace_path: Path | None = None
    kent_home: Path | None = None
    lightning_store: Path = field(
        default_factory=lambda: Path.home() / ".kent" / "lightning_store"
    )
    n_runners: int = 4
    max_rounds: int = 20
    train_size: int = 50
    val_size: int = 20
    current_resource: str = ""  # set by train_resource() before fit() so the rollout knows which resource is being optimized (others are frozen)
