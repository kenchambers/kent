import os
import pytest
from pydantic import BaseModel
from agent import run, ToolRegistry, ToolResult, Terminal, OpenAICompatibleLLM
from agent.events import TextDelta

pytestmark = pytest.mark.integration


@pytest.fixture
def ollama_llm():
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct")
    return OpenAICompatibleLLM(
        base_url=f"{host}/v1",
        api_key="ollama",
        model=model,
        context_window=8192,
    )


@pytest.mark.asyncio
async def test_ollama_simple_qa(ollama_llm):
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
        tools=ToolRegistry(),
        llm=ollama_llm,
        max_turns=2,
    ):
        events.append(ev)

    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason == "completed"
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "4" in text


@pytest.mark.asyncio
async def test_ollama_with_tool(ollama_llm):
    class ReadFile:
        name = "read_file"
        description = "Read a file from disk"
        class Args(BaseModel):
            path: str
        input_model = Args
        def is_concurrency_safe(self, args): return True
        async def call(self, args, ctx):
            try:
                with open(args.path) as f:
                    return ToolResult(call_id="", output=f.read()[:500])
            except FileNotFoundError:
                return ToolResult(call_id="", output=f"File not found: {args.path}", is_error=True)

    registry = ToolRegistry()
    registry.register(ReadFile())
    events = []
    async for ev in run(
        messages=[{"role": "user", "content": "Read /etc/hostname and tell me what it says."}],
        tools=registry,
        llm=ollama_llm,
        max_turns=5,
    ):
        events.append(ev)

    terminal = next(e for e in events if isinstance(e, Terminal))
    assert terminal.reason in ("completed", "max_turns")
