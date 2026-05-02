import asyncio
import queue
import threading
from pathlib import Path

from agent.llm import LLM
from agent.tools import ToolRegistry
from agent.builtin.shell import Shell
from agent.builtin.web_fetch import WebFetch
from agent.builtin.web_search import WebSearch
from agent.builtin.memory_recall import MemoryRecall
from agent.builtin.memory_recall_here import MemoryRecallHere
from agent.builtin.diary_write import DiaryWrite
from agent.builtin.set_wing import SetWing
from agent.builtin.tunnel_create import TunnelCreate
from agent.builtin.code_drawer import CodeDrawer
from agent.builtin.closet_refresh import ClosetRefresh
from agent.loop import run as agent_run
from agent.memory.mempalace_store import MemPalaceStore


class ChatSession:
    """Thin wrapper around agent.loop.run for the viz chat panel.

    Owns one MemPalaceStore + ToolRegistry + conversation history. Each
    .send(message) call drives one agent turn-loop and yields normalized
    event dicts (type, data) the SSE handler forwards verbatim.

    Only one .send() may run at a time — guarded by a lock. Concurrent
    POSTs from the same browser are serialized.
    """

    def __init__(self, *, llm: LLM, palace: Path, kent_home: Path):
        self.llm = llm
        self.store = MemPalaceStore(palace_path=palace, kent_home=kent_home)
        self.tools = self._build_tools()
        self.system = _system_prompt()
        self.history: list[dict] = []
        # Seed with global + wing-scoped recall so the chat session inherits
        # prior memory the same way `kent` REPL / `kent run` do.
        recalled = self.store.wake_up_full()
        if recalled:
            self.history.append(
                {"role": "system", "content": f"<recalled-memory>{recalled}</recalled-memory>"}
            )
        self._lock = threading.Lock()

    def send(self, user_message: str):
        """Generator of {type, data} dicts. Synchronous facade over the
        async agent loop — uses a queue + bg event-loop thread."""
        with self._lock:
            self.history.append({"role": "user", "content": user_message})
            q: queue.Queue = queue.Queue()
            STOP = object()

            async def _drive():
                try:
                    async for ev in agent_run(
                        messages=list(self.history),
                        tools=self.tools,
                        llm=self.llm,
                        system=self.system,
                        max_turns=20,
                        memory_store=self.store,
                    ):
                        q.put(_to_dict(ev))
                        if ev.__class__.__name__ == "AssistantMessageComplete":
                            self.history.append(ev.message.to_openai_dict())
                finally:
                    q.put(STOP)

            t = threading.Thread(
                target=lambda: asyncio.new_event_loop().run_until_complete(_drive()),
                daemon=True,
            )
            t.start()

            while True:
                ev = q.get()
                if ev is STOP:
                    break
                yield ev

    def _build_tools(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(Shell())
        reg.register(WebSearch())
        reg.register(WebFetch())
        # Memory + diary + wing tools — without these the chat panel can't
        # mint new drawers, which is the central UX promise of the plan.
        reg.register(MemoryRecall(self.store))
        reg.register(MemoryRecallHere(self.store))
        reg.register(DiaryWrite(self.store))
        reg.register(SetWing(self.store))
        reg.register(TunnelCreate())
        reg.register(CodeDrawer(self.store.palace_path, self.store.active_wing))
        base_url = api_key = model = None
        try:
            client = getattr(self.llm, "client", None)
            if client is not None:
                base_url = str(getattr(client, "base_url", "")) or None
                api_key = getattr(client, "api_key", None) or None
            model = getattr(self.llm, "model", None)
        except Exception:
            pass
        reg.register(ClosetRefresh(
            self.store.palace_path, self.store.active_wing,
            base_url=base_url, api_key=api_key, model=model,
        ))
        return reg


def _system_prompt() -> str:
    return (
        "You are kent, a terminal AI agent. You have these tools: "
        "web_search (DuckDuckGo HTML, no API key), web_fetch (URL → markdown), "
        "shell (host shell — bash on macOS/Linux/WSL, PowerShell on Windows), "
        "memory_recall (search long-term memory from previous sessions), "
        "memory_recall_here (search the active project wing's diary), "
        "diary_write (record observations, findings, decisions, patterns), "
        "set_wing (switch or register a named project context), "
        "tunnel_create (draw a persistent labeled edge between two rooms across wings — "
        "use it when you spot a relationship the sweeper wouldn't catch on its own). "
        "Prefer web_search before web_fetch. Prefer shell over re-implementing with another tool. "
        "Use memory_recall when the user asks about things you might have discussed before. "
        "Use memory_recall_here for project-specific context. "
        "Use diary_write to capture important observations, decisions, or patterns. "
        "Use tunnel_create when two memories belong together — it makes the connection visible "
        "in the palace graph. "
        "Keep responses concise."
    )


def _to_dict(ev) -> dict:
    """Normalize an agent event into {type, data} for the SSE wire."""
    name = ev.__class__.__name__
    if name == "TextDelta":
        return {"type": name, "data": {"text": ev.text}}
    if name == "ToolCallComplete":
        c = ev.call
        return {"type": name, "data": {"name": c.name, "arguments": c.arguments}}
    if name == "ToolResult":
        return {"type": name, "data": {"call_id": ev.call_id, "ok": not ev.is_error}}
    if name == "AssistantMessageComplete":
        return {"type": name, "data": {}}
    if name == "ModelError":
        return {"type": name, "data": {"error": str(ev.error)}}
    if name == "Terminal":
        return {"type": name, "data": {"reason": ev.reason}}
    return {"type": name, "data": {}}
