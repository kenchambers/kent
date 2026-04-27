"""
Optional second-pass critic. Off by default.

Runs after the main agent finishes a turn. If the critic flags issues,
the caller injects the critique as the next user message so the main
agent revises. One round only — diminishing returns past that.

Per arxiv:2510.08308 ("First Try Matters"), external reflection helps
non-reasoning models (~+9% on hard reasoning) but is largely redundant
for thinking models (o1/R1/extended-thinking). Leave disabled for those.
"""
from __future__ import annotations

from .events import TextDelta
from .llm import LLM
from .state import Message

CRITIC_SYSTEM = (
    "You are a critic. Read the conversation and judge whether the "
    "assistant's final answer is correct, complete, and free of "
    "hallucinations.\n\n"
    "Respond with exactly one of:\n"
    "  PASS — the answer is correct and complete\n"
    "  ISSUES: <bullet list of specific problems> — otherwise\n\n"
    "Be strict on factual claims, code correctness, and unaddressed parts "
    "of the user's request. Be lenient on style. Do not rewrite the "
    "answer — only flag issues for the assistant to fix."
)


async def critique(messages: list[Message], llm: LLM) -> str | None:
    """
    Run one critic pass. Returns None on PASS, otherwise the critique
    text to inject as the next user message.
    """
    transcript = _format(messages)
    if not transcript:
        return None
    parts: list[str] = []
    async for ev in llm.stream(
        [{"role": "user", "content": f"Conversation:\n\n{transcript}\n\nVerdict:"}],
        tools=[],
        system=CRITIC_SYSTEM,
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
    text = "".join(parts).strip()
    if not text or text.upper().startswith("PASS"):
        return None
    return text


def _format(messages: list[Message]) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue  # skip tool calls/results — noise for the critic
        content = m.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)
