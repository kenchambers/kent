from __future__ import annotations

import fcntl
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DiaryKind = Literal["OBSERVATION", "FINDING", "DECISION", "PATTERN"]


class DiaryWriter:
    """Appends entries to per-wing daily Markdown files and triggers mempalace ingest."""

    def __init__(self, palace_path: Path, kent_home: Path) -> None:
        self._palace_path = palace_path
        self._kent_home = kent_home

    def _diary_file(self, wing: str) -> Path:
        diary_dir = self._kent_home / "diaries" / wing
        diary_dir.mkdir(parents=True, exist_ok=True)
        return diary_dir / f"{date.today().isoformat()}.md"

    def write(self, wing: str, kind: str, text: str, topic: str | None = None) -> None:
        path = self._diary_file(wing)
        ts = datetime.now().strftime("%H:%M:%S")
        topic_suffix = f" {topic}" if topic else ""
        header = f"## {ts} [agent=kent] [{kind}]{topic_suffix}\n"
        body = text.rstrip() + "\n\n"
        entry = header + body

        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                if os.fstat(f.fileno()).st_size == 0:
                    f.write(f"# {date.today().isoformat()}\n\n")
                f.write(entry)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        self._ingest(wing)

    def _ingest(self, wing: str) -> None:
        try:
            from mempalace.diary_ingest import ingest_diaries

            diary_dir = self._kent_home / "diaries" / wing
            ingest_diaries(
                diary_dir=str(diary_dir),
                palace_path=str(self._palace_path),
                wing=wing,
                force=False,
            )
        except Exception:
            logger.warning("DiaryWriter._ingest failed", exc_info=True)
