from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext


class TunnelCreate:
    """Draw an explicit edge between two locations in the palace.

    Tunnels are persistent, undirected links stored in
    ``~/.mempalace/tunnels.json``. They appear as red animated edges in the
    3D viz and let kent surface relationships the sweeper can't infer
    automatically (e.g. "this auth bug in wing 'api' is the same root cause
    as the timeout in wing 'frontend'").
    """

    name = "tunnel_create"
    description = (
        "Connect two rooms in the palace with a labeled, persistent edge. "
        "Use this when you notice a relationship across wings or rooms that "
        "the sweeper wouldn't catch — e.g. two drawers about the same bug, "
        "two patterns that share a root cause, an old finding that resolves a "
        "new question. Calling twice with the same endpoints updates the label "
        "(no duplicates). Required: source_wing, source_room, target_wing, "
        "target_room. Optional: label (one-line description)."
    )

    class Args(BaseModel):
        source_wing: str
        source_room: str
        target_wing: str
        target_room: str
        label: str = ""

    input_model = Args

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        try:
            from mempalace.palace_graph import create_tunnel
        except ImportError:
            return ToolResult(
                call_id="",
                output="tunnel_create requires mempalace; install with: uv pip install mempalace",
                is_error=True,
            )

        try:
            t = create_tunnel(
                source_wing=args.source_wing,
                source_room=args.source_room,
                target_wing=args.target_wing,
                target_room=args.target_room,
                label=args.label,
            )
        except ValueError as e:
            return ToolResult(call_id="", output=str(e), is_error=True)
        except Exception as e:
            return ToolResult(
                call_id="",
                output=f"tunnel_create failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        endpoints = (
            f"{args.source_wing}/{args.source_room} ↔ "
            f"{args.target_wing}/{args.target_room}"
        )
        label_part = f" — {args.label!r}" if args.label else ""
        return ToolResult(
            call_id="",
            output=f"[tunnel] {endpoints}{label_part} (id={t.get('id', '?')})",
        )
