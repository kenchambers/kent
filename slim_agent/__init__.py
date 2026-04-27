"""
slim_agent — minimal async agent runtime for OpenAI-compatible LLMs.
Entry point: run(). Build tools via the Tool protocol and ToolRegistry.
"""
from .loop import run
from .state import LoopState, TerminalReason, TransitionReason, Message
from .llm import LLM, OpenAICompatibleLLM, ContextOverflowError
from .tools import Tool, ToolRegistry, ToolContext, CanUseToolFn
from .compact import maybe_compact
from .events import (
    TurnStart, TextDelta, ThinkingDelta,
    ToolCall, ToolCallStart, ToolCallDelta, ToolCallComplete,
    AssistantMessage, AssistantMessageComplete,
    ToolResult, ContextOverflow, ModelError,
    MaxTurnsReached, ToolLoopDetected, Terminal,
)
from .builtin.spawn import Spawn
from .builtin.web_search import WebSearch
from .builtin.web_fetch import WebFetch
from .builtin.shell import Shell, ShellBackend, detect_shell_backend

__all__ = [
    "run",
    "LoopState", "TerminalReason", "TransitionReason", "Message",
    "LLM", "OpenAICompatibleLLM", "ContextOverflowError",
    "Tool", "ToolRegistry", "ToolContext", "CanUseToolFn",
    "maybe_compact",
    "TurnStart", "TextDelta", "ThinkingDelta",
    "ToolCall", "ToolCallStart", "ToolCallDelta", "ToolCallComplete",
    "AssistantMessage", "AssistantMessageComplete",
    "ToolResult", "ContextOverflow", "ModelError",
    "MaxTurnsReached", "ToolLoopDetected", "Terminal",
    "Spawn", "WebSearch", "WebFetch",
    "Shell", "ShellBackend", "detect_shell_backend",
]
