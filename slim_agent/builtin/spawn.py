from pydantic import BaseModel
from typing import TYPE_CHECKING

from ..events import AssistantMessageComplete, ToolResult
from ..tools import ToolRegistry, ToolContext
from ..loop import run as agent_run

if TYPE_CHECKING:
    from ..llm import LLM

SUBAGENT_SYSTEM_PROMPT = (
    "You are a focused subagent. Complete the given task precisely and concisely. "
    "When done, output your final answer as plain text."
)
class SpawnArgs(BaseModel):
    instructions: str
    tools: list[str] | None = None  # None = all parent tools except Spawn; [] = no tools


class Spawn:
    """
    Delegate a subtask to a fresh agent with its own context window.
    tools=None (default) gives the subagent all parent tools except Spawn itself.
    tools=[] gives no tools. tools=[...] gives exactly those named tools.
    """
    name = "spawn_subagent"
    description = (
        "Delegate a focused subtask to a fresh agent with its own context window. "
        "Returns the subagent's final answer as text. Use when: (a) the subtask "
        "would consume significant tokens you don't want polluting the main thread, "
        "(b) the subtask is independent enough to parallelize with multiple spawn calls, "
        "(c) the subtask needs different tools."
    )
    input_model = SpawnArgs

    def __init__(self, parent_registry: ToolRegistry, llm: "LLM"):
        self.parent_registry = parent_registry
        self.llm = llm

    def is_concurrency_safe(self, args: SpawnArgs) -> bool:
        return True  # subagents parallelize

    async def call(self, args: SpawnArgs, ctx: ToolContext) -> ToolResult:
        sub_tools = ToolRegistry()
        if args.tools is None:
            # Default: all parent tools except Spawn itself
            for name in self.parent_registry.names():
                t = self.parent_registry.get(name)
                if t and t.name != self.name:
                    sub_tools.register(t)
        else:
            for name in args.tools:
                t = self.parent_registry.get(name)
                if t and t.name != self.name:
                    sub_tools.register(t)

        sub_messages = [{"role": "user", "content": args.instructions}]
        final_text: list[str] = []

        async for ev in agent_run(
            messages=sub_messages,
            tools=sub_tools,
            llm=self.llm,
            system=SUBAGENT_SYSTEM_PROMPT,
            max_turns=10,
            signal=ctx.signal,
        ):
            if isinstance(ev, AssistantMessageComplete):
                final_text = [ev.message.content] if ev.message.content else []

        return ToolResult(
            call_id="",  # will be set by caller
            output="\n".join(final_text) if final_text else "",
        )
