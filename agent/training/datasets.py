from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TrainingExample:
    task_id: str
    prompt: str
    expected_outcome: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def load_examples(path: Path) -> list[TrainingExample]:
    """Load JSONL training examples from a file."""
    examples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            examples.append(TrainingExample(
                task_id=data["task_id"],
                prompt=data["prompt"],
                expected_outcome=data.get("expected_outcome", ""),
                metadata=data.get("metadata", {}),
            ))
    return examples


def load_examples_dir(directory: Path) -> list[TrainingExample]:
    """Load all JSONL examples from a directory."""
    examples = []
    for path in sorted(directory.glob("*.jsonl")):
        examples.extend(load_examples(path))
    return examples
