from __future__ import annotations

import os
import re
from pathlib import Path

_WING_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def kent_home() -> Path:
    return Path(os.environ.get("KENT_HOME", str(Path.home() / ".kent")))


def diaries_root(*, home: Path | None = None) -> Path:
    return (home if home is not None else kent_home()) / "diaries"


def wing_dir(name: str, *, home: Path | None = None) -> Path:
    return diaries_root(home=home) / name


def sanitize_wing(name: str) -> str:
    """Validate and return lowercased wing name. Raises ValueError if invalid."""
    lowered = name.lower()
    if not _WING_RE.match(lowered):
        raise ValueError(
            f"Invalid wing name {name!r}. "
            "Must match ^[a-z0-9][a-z0-9_-]{0,63}$ (lowercase alphanumeric, _ or -, "
            "1–64 chars, must start with letter or digit)."
        )
    return lowered


def list_wings(*, home: Path | None = None) -> list[str]:
    """Return sorted list of wing names (directory names under diaries_root)."""
    root = diaries_root(home=home)
    if not root.exists():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and _WING_RE.match(d.name)
    )


def read_active_wing(default: str = "kent_default", *, home: Path | None = None) -> str:
    h = home if home is not None else kent_home()
    path = h / "active_wing.txt"
    if path.exists():
        name = path.read_text().strip()
        if name:
            return name
    return default


def write_active_wing(name: str, *, home: Path | None = None) -> None:
    h = home if home is not None else kent_home()
    h.mkdir(parents=True, exist_ok=True)
    (h / "active_wing.txt").write_text(name)


def read_intent(name: str, *, home: Path | None = None) -> str | None:
    intent_file = wing_dir(name, home=home) / ".intent.txt"
    if intent_file.exists():
        text = intent_file.read_text().strip()
        return text or None
    return None


def write_intent(name: str, text: str, *, home: Path | None = None) -> None:
    d = wing_dir(name, home=home)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".intent.txt").write_text(text)
