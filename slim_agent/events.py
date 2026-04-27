import json
from dataclasses import dataclass
from typing import Any

@dataclass(slots=True, frozen=True)
class TurnStart:
    turn: int

@dataclass(slots=True, frozen=True)
class TextDelta:
    text: str

@dataclass(slots=True, frozen=True)
class ThinkingDelta:
    text: str

@dataclass(slots=True, frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

@dataclass(slots=True, frozen=True)
class ToolCallStart:
    call_id: str
    name: str

@dataclass(slots=True, frozen=True)
class ToolCallDelta:
    call_id: str
    args_json_delta: str

@dataclass(slots=True, frozen=True)
class ToolCallComplete:
    call: ToolCall

@dataclass(slots=True, frozen=True)
class AssistantMessage:
    content: str
    tool_calls: list[ToolCall]

    def to_openai_dict(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return msg

@dataclass(slots=True, frozen=True)
class AssistantMessageComplete:
    message: AssistantMessage

@dataclass(slots=True, frozen=True)
class ToolResult:
    call_id: str
    output: Any
    is_error: bool = False

    def to_message(self) -> dict:
        content = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": content,
        }

@dataclass(slots=True, frozen=True)
class ContextOverflow:
    error: Exception

@dataclass(slots=True, frozen=True)
class ModelError:
    error: Exception

@dataclass(slots=True, frozen=True)
class MaxTurnsReached:
    turn: int

@dataclass(slots=True, frozen=True)
class ToolLoopDetected:
    calls: list[ToolCall]

@dataclass(slots=True, frozen=True)
class Terminal:
    reason: str  # TerminalReason
