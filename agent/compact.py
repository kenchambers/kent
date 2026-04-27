from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLM
    from .state import LoopState, Message
    from .memory.store import MemoryStore

COMPACT_THRESHOLD = 0.85
COMPACT_KEEP_TAIL = 6
SUMMARIZE_SYSTEM = (
    "You are a summarization assistant. Summarize the following conversation "
    "history concisely, preserving key facts, decisions, and tool results that "
    "future turns will need. Output only the summary text."
)


async def _summarize(messages: list["Message"], llm: "LLM") -> str:
    from .events import TextDelta, AssistantMessageComplete
    text_parts = []
    async for ev in llm.stream(messages, [], SUMMARIZE_SYSTEM):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)
        elif isinstance(ev, AssistantMessageComplete):
            break
    return "".join(text_parts)


async def maybe_compact(
    state: "LoopState",
    llm: "LLM",
    *,
    memory_store: "MemoryStore | None" = None,
) -> "LoopState":
    """Summarize the head of state.messages when the context exceeds COMPACT_THRESHOLD."""
    used = llm.count_tokens(list(state.messages)) / llm.context_window
    if used < COMPACT_THRESHOLD:
        return state

    # Nothing to compact away if everything fits in the tail.
    if len(state.messages) <= COMPACT_KEEP_TAIL:
        return state

    head = list(state.messages[:-COMPACT_KEEP_TAIL])
    tail = list(state.messages[-COMPACT_KEEP_TAIL:])

    summary = await _summarize(head, llm)
    parts = [f"<conversation-summary>{summary}</conversation-summary>"]
    if memory_store:
        recalled = memory_store.wake_up()
        if recalled:
            parts.append(f"<recalled-memory>{recalled}</recalled-memory>")

    summary_msg: "Message" = {
        "role": "system",
        "content": "\n".join(parts),
    }
    new_messages = (summary_msg, *tail)
    return state.advance(messages=new_messages)
