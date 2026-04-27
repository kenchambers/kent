from dataclasses import dataclass, replace
from typing import Literal, Any

Message = dict[str, Any]  # OpenAI message dict

TerminalReason = Literal[
    "completed", "max_turns", "aborted",
    "model_error", "context_overflow", "tool_loop",
]
TransitionReason = Literal[
    "next_turn", "compact_retry", "context_overflow_recovery",
    "tool_loop_break",
]

@dataclass(frozen=True, slots=True)
class LoopState:
    """
    Immutable snapshot of the agent loop. Treat as read-only from outside run().
    messages is the full conversation so far; turn is zero-indexed.
    """
    messages: tuple[Message, ...]
    turn: int = 0
    attempted_compact_recovery: bool = False
    last_transition: TransitionReason | None = None

    def advance(self, **changes) -> "LoopState":
        return replace(self, **changes)
