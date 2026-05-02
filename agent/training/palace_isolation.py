import os
import shutil
import uuid
from pathlib import Path

ROLLOUT_BASE = Path.home() / ".mempalace" / "_rollouts"


class IsolationError(RuntimeError):
    """Raised when scratch isolation invariants are violated."""


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink when possible; fall back to copy across filesystems."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def snapshot(kent_home: Path, palace_path: Path) -> tuple[str, Path]:
    """Create isolated scratch for one rollout. Returns (rollout_id, scratch_root)."""
    rollout_id = uuid.uuid4().hex
    scratch = ROLLOUT_BASE / rollout_id
    scratch_palace = scratch / "palace"
    scratch_palace.mkdir(parents=True, exist_ok=True)

    if palace_path.exists():
        for src in palace_path.iterdir():
            dst = scratch_palace / src.name
            if src.name == "chroma.sqlite3":
                shutil.copy2(src, dst)
            elif src.is_file():
                _link_or_copy(src, dst)
            elif src.is_dir():
                shutil.copytree(src, dst, symlinks=False)

    diaries_src = kent_home / "diaries"
    if diaries_src.exists():
        shutil.copytree(diaries_src, scratch / "diaries", symlinks=False)
    else:
        (scratch / "diaries").mkdir(parents=True, exist_ok=True)

    wing_src = kent_home / "active_wing.txt"
    if wing_src.exists():
        shutil.copy2(wing_src, scratch / "active_wing.txt")
    else:
        (scratch / "active_wing.txt").write_text("default\n")

    # Copy tunnels.json for inspection/assertion; rollout TunnelCreate is a no-op
    # during training (see rollout.py), so this file is read-only in the scratch.
    tunnels_src = Path.home() / ".mempalace" / "tunnels.json"
    if tunnels_src.exists():
        shutil.copy2(tunnels_src, scratch / "tunnels.json")

    return rollout_id, scratch


def cleanup(rollout_id: str) -> None:
    """Remove scratch directory for a rollout."""
    scratch = ROLLOUT_BASE / rollout_id
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)


def assert_isolation(kent_home: Path, palace_path: Path, scratch: Path) -> None:
    """Verify scratch files are not hardlinked to source. Raises IsolationError on violation."""
    src_sqlite = palace_path / "chroma.sqlite3"
    dst_sqlite = scratch / "palace" / "chroma.sqlite3"
    if src_sqlite.exists() and dst_sqlite.exists():
        if src_sqlite.stat().st_ino == dst_sqlite.stat().st_ino:
            raise IsolationError("chroma.sqlite3 must not be hardlinked from base palace")

    src_diaries = kent_home / "diaries"
    dst_diaries = scratch / "diaries"
    if src_diaries.exists():
        for src_diary in src_diaries.rglob("*.md"):
            rel = src_diary.relative_to(src_diaries)
            dst_diary = dst_diaries / rel
            if dst_diary.exists():
                if src_diary.stat().st_ino == dst_diary.stat().st_ino:
                    raise IsolationError(f"Diary {rel} must not be hardlinked from base diaries")
                break

    src_tunnels = Path.home() / ".mempalace" / "tunnels.json"
    dst_tunnels = scratch / "tunnels.json"
    if src_tunnels.exists() and dst_tunnels.exists():
        if src_tunnels.stat().st_ino == dst_tunnels.stat().st_ino:
            raise IsolationError("tunnels.json must not be hardlinked from base mempalace")
