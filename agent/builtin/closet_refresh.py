from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..events import ToolResult

if TYPE_CHECKING:
    from ..tools import ToolContext

logger = logging.getLogger(__name__)


class ClosetRefresh:
    """Rebuild closets for the active (or specified) wing via LLM or regex fallback."""

    name = "closet_refresh"
    description = (
        "Rebuild memory closets for the active wing. "
        "Uses LLM summarization when an endpoint is configured; "
        "falls back to regex if not. Not concurrency-safe."
    )

    class Args(BaseModel):
        wing: str | None = None

    input_model = Args

    def __init__(self, palace_path: Path, active_wing: str, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> None:
        self._palace_path = palace_path
        self._active_wing = active_wing
        self._base_url = base_url
        self._api_key = api_key
        self._model = model

    def is_concurrency_safe(self, args: Args) -> bool:
        return False

    async def call(self, args: Args, ctx: "ToolContext") -> ToolResult:
        wing = args.wing or self._active_wing
        try:
            from mempalace.closet_llm import regenerate_closets, LLMConfig
            from mempalace.palace_graph import invalidate_graph_cache

            cfg = None
            if self._base_url and self._api_key:
                cfg = LLMConfig(
                    endpoint=self._base_url,
                    key=self._api_key,
                    model=self._model,
                )

            regenerate_closets(str(self._palace_path), wing=wing, cfg=cfg)
            invalidate_graph_cache()
        except Exception as e:
            logger.warning("closet_refresh failed", exc_info=True)
            return ToolResult(call_id="", output=f"[closet_refresh] error: {e}", is_error=True)

        wing_note = f"wing={wing}" if wing else "all wings"
        return ToolResult(call_id="", output=f"[closet_refresh] closets rebuilt for {wing_note}")
