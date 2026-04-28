import json
import logging
import re
from dataclasses import dataclass
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]+?)```")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

logger = logging.getLogger(__name__)

CRITIC_SYSTEM = """You are an evaluator for an AI agent called kent.
Score the agent's response on four axes. Return ONLY valid JSON, no other text.

{
  "task_success": 0 or 1,
  "reasoning_quality": float 0..1,
  "tool_efficiency": float 0..1,
  "memory_use": float 0..1,
  "rationale": "one sentence explaining the score"
}

task_success=1 only if the agent fully completed the user's request.
reasoning_quality: clarity and correctness of reasoning steps.
tool_efficiency: minimal unnecessary tool calls.
memory_use: appropriate use of memory recall and diary tools."""


@dataclass
class CriticScore:
    task_success: int
    reasoning_quality: float
    tool_efficiency: float
    memory_use: float
    rationale: str

    def scalar(self) -> float:
        return _clamp01(
            0.5 * self.task_success
            + 0.2 * self.reasoning_quality
            + 0.15 * self.tool_efficiency
            + 0.15 * self.memory_use
        )


async def score_rollout(messages: list[dict], critic_llm: Any) -> CriticScore:
    """Call critic LLM, parse JSON, return CriticScore."""
    from agent.events import TextDelta

    transcript = _format_transcript(messages)
    parts: list[str] = []
    async for ev in critic_llm.stream(
        [{"role": "user", "content": f"Agent conversation to evaluate:\n\n{transcript}"}],
        tools=[],
        system=CRITIC_SYSTEM,
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)

    raw = "".join(parts).strip()
    return _parse_score(raw)


def _parse_score(raw: str) -> CriticScore:
    try:
        text = raw
        m = _FENCE_RE.search(text)
        if m:
            text = m.group(1)
        data = json.loads(text.strip())
        ts = int(data.get("task_success", 0))
        return CriticScore(
            task_success=1 if ts >= 1 else 0,
            reasoning_quality=_clamp01(float(data.get("reasoning_quality", 0.5))),
            tool_efficiency=_clamp01(float(data.get("tool_efficiency", 0.5))),
            memory_use=_clamp01(float(data.get("memory_use", 0.5))),
            rationale=str(data.get("rationale", "")),
        )
    except Exception:
        logger.warning("Critic score parse failed for: %r", raw[:200])
        return CriticScore(
            task_success=0,
            reasoning_quality=0.0,
            tool_efficiency=0.0,
            memory_use=0.0,
            rationale="parse_error",
        )


def _format_transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        lines.append(f"[{role.upper()}] {content}")
    return "\n".join(lines)
