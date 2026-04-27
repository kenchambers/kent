from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult
from ..memory.wings import list_wings, read_intent, sanitize_wing, wing_dir, write_intent

if TYPE_CHECKING:
    from ..memory.store import WingedMemoryStore
    from ..tools import ToolContext


class SetWing:
    """Switch to a named project wing or register a new one."""

    name = "set_wing"
    description = (
        "Switch to a named project wing or register a new one. "
        "If the wing already exists, switches immediately. "
        "If the wing is new, requires intent='short description' to register it — "
        "when intent is missing, ask the user to confirm the name and description, "
        "then re-call with intent='...'."
    )

    class Args(BaseModel):
        name: str
        intent: str | None = None

    input_model = Args

    def __init__(self, store: "WingedMemoryStore") -> None:
        self._store = store

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        try:
            clean = sanitize_wing(args.name)
        except ValueError as e:
            return ToolResult(call_id="", output=str(e), is_error=True)

        home = self._store.kent_home
        existing = list_wings(home=home)

        if clean in existing:
            self._store.set_active_wing(clean)
            intent = read_intent(clean, home=home)
            intent_part = f" ({intent})" if intent else ""
            return ToolResult(
                call_id="",
                output=f"[wing] switched to {clean!r}{intent_part}.",
            )

        if not args.intent:
            return ToolResult(
                call_id="",
                output=(
                    f"Wing {clean!r} doesn't exist. "
                    "Confirm the name with the user and ask for a one-line project description, "
                    f"then re-call: set_wing(name={clean!r}, intent='...')."
                ),
                is_error=True,
            )

        intent_text = args.intent.strip()
        write_intent(clean, intent_text, home=home)
        wing_dir(clean, home=home).mkdir(parents=True, exist_ok=True)
        self._store.set_active_wing(clean)
        return ToolResult(
            call_id="",
            output=f"[wing] registered and switched to {clean!r}: {intent_text!r}.",
        )
