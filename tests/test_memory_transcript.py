"""Round-trip tests for JSONL transcript writer."""
import importlib.util
import json
import pytest
from pathlib import Path

from agent.memory.transcript import append_messages


def test_writes_one_line_per_message(tmp_path):
    path = tmp_path / "session.jsonl"
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
        {"role": "user", "content": "thanks"},
    ]
    append_messages(path, messages, session_id="test-sid")
    lines = [l for l in path.read_text().strip().split("\n") if l]
    assert len(lines) == 3


def test_record_fields_present(tmp_path):
    path = tmp_path / "session.jsonl"
    append_messages(
        path,
        [{"role": "user", "content": "hi"}],
        session_id="sid-001",
    )
    rec = json.loads(path.read_text().strip())
    assert rec["sessionId"] == "sid-001"
    assert "uuid" in rec
    assert "timestamp" in rec
    assert "message" in rec
    assert rec["message"]["role"] == "user"
    assert rec["message"]["content"] == "hi"


def test_assistant_with_tool_calls_encodes_as_blocks(tmp_path):
    path = tmp_path / "session.jsonl"
    msg = {
        "role": "assistant",
        "content": "thinking...",
        "tool_calls": [
            {"id": "tc1", "function": {"name": "my_tool", "arguments": '{"x": 1}'}},
        ],
    }
    append_messages(path, [msg], session_id="s1")
    rec = json.loads(path.read_text().strip())
    content = rec["message"]["content"]
    assert isinstance(content, list)
    assert any(b["type"] == "text" and b["text"] == "thinking..." for b in content)
    assert any(
        b["type"] == "tool_use" and b["name"] == "my_tool" and b["input"] == {"x": 1}
        for b in content
    )


def test_tool_result_encodes_as_user_with_tool_result_block(tmp_path):
    path = tmp_path / "session.jsonl"
    msg = {"role": "tool", "tool_call_id": "tc1", "content": "the result"}
    append_messages(path, [msg], session_id="s1")
    rec = json.loads(path.read_text().strip())
    assert rec["type"] == "user"
    assert rec["message"]["role"] == "user"
    content = rec["message"]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "tool_result"
    assert content[0]["tool_use_id"] == "tc1"
    assert content[0]["content"] == "the result"


def test_plain_assistant_encodes_as_string(tmp_path):
    path = tmp_path / "session.jsonl"
    msg = {"role": "assistant", "content": "hello", "tool_calls": []}
    append_messages(path, [msg], session_id="s1")
    rec = json.loads(path.read_text().strip())
    assert rec["type"] == "assistant"
    assert rec["message"]["content"] == "hello"


def test_append_is_cumulative(tmp_path):
    path = tmp_path / "session.jsonl"
    append_messages(path, [{"role": "user", "content": "a"}], session_id="s1")
    append_messages(path, [{"role": "assistant", "content": "b"}], session_id="s1")
    lines = [l for l in path.read_text().strip().split("\n") if l]
    assert len(lines) == 2


def test_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "session.jsonl"
    append_messages(path, [{"role": "user", "content": "x"}], session_id="s1")
    assert path.exists()


def test_uuid_unique_per_message(tmp_path):
    path = tmp_path / "session.jsonl"
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    append_messages(path, msgs, session_id="s1")
    lines = [l for l in path.read_text().strip().split("\n") if l]
    uuids = [json.loads(l)["uuid"] for l in lines]
    assert len(set(uuids)) == 5


def test_assistant_no_content_tool_calls(tmp_path):
    """Assistant message with tool calls but no text content."""
    path = tmp_path / "session.jsonl"
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "tc1", "function": {"name": "search", "arguments": '{"q": "test"}'}},
        ],
    }
    append_messages(path, [msg], session_id="s1")
    rec = json.loads(path.read_text().strip())
    content = rec["message"]["content"]
    assert isinstance(content, list)
    assert not any(b["type"] == "text" for b in content)
    assert any(b["type"] == "tool_use" for b in content)


@pytest.mark.skipif(
    not importlib.util.find_spec("mempalace"),
    reason="mempalace not installed",
)
def test_round_trip_parse_claude_jsonl(tmp_path):
    """Write JSONL and verify mempalace.sweeper.parse_claude_jsonl reads it back."""
    from mempalace.sweeper import parse_claude_jsonl

    path = tmp_path / "session.jsonl"
    messages = [
        {"role": "user", "content": "question about octarine"},
        {"role": "assistant", "content": "octarine is a fictional colour"},
    ]
    append_messages(path, messages, session_id="round-trip-session")

    records = list(parse_claude_jsonl(path))
    assert len(records) == 2

    # Strict shape: every record carries session_id, uuid, timestamp, role, content
    for r in records:
        assert r["session_id"] == "round-trip-session"
        assert isinstance(r["uuid"], str) and len(r["uuid"]) > 0
        assert isinstance(r["timestamp"], str) and len(r["timestamp"]) > 0
        assert r["role"] in ("user", "assistant")
        assert isinstance(r["content"], str) and len(r["content"]) > 0

    # Per-message uniqueness on uuid
    uuids = [r["uuid"] for r in records]
    assert len(set(uuids)) == len(uuids)

    # Round-trip preserves order and content fidelity
    assert records[0]["role"] == "user"
    assert "octarine" in records[0]["content"]
    assert records[1]["role"] == "assistant"
    assert "fictional colour" in records[1]["content"]
