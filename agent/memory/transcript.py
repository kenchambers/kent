from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _new_uuid() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _encode_content(msg: dict) -> Any:
    """Convert an OpenAI-format message to Claude-Code-format content."""
    role = msg.get("role", "user")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")

    if role == "assistant" and tool_calls:
        blocks: list[dict] = []
        if content:
            blocks.append({"type": "text", "text": content})
        for tc in tool_calls:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(args) if isinstance(args, str) else args
            except (json.JSONDecodeError, TypeError):
                parsed_args = {}
            blocks.append({
                "type": "tool_use",
                "name": fn.get("name", ""),
                "input": parsed_args,
            })
        return blocks

    if role == "tool":
        return [
            {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content or "",
            }
        ]

    return content


def append_messages(path: Path, messages: list[dict], *, session_id: str) -> None:
    """Append messages to a JSONL file in Claude-Code-compatible format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for msg in messages:
            role = msg.get("role", "user")
            rec_type = "user" if role == "tool" else role
            record = {
                "type": rec_type,
                "sessionId": session_id,
                "uuid": _new_uuid(),
                "timestamp": _now_iso(),
                "message": {
                    "role": "user" if role == "tool" else role,
                    "content": _encode_content(msg),
                },
            }
            f.write(json.dumps(record) + "\n")
        f.flush()
