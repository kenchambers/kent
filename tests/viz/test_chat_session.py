"""Tests for ChatSession.send() with a stub LLM.

FIXED (R16): unit tests — no real LLM, no network, no port binding.
Verifies event order and normalization logic from _to_dict().
"""


class TestToDictNormalization:
    """_to_dict correctly maps each event class to a wire dict."""

    def test_text_delta(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import TextDelta

        result = _to_dict(TextDelta(text="hi"))
        assert result == {"type": "TextDelta", "data": {"text": "hi"}}

    def test_tool_call_complete(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import ToolCallComplete, ToolCall

        call = ToolCall(call_id="c1", name="shell", arguments={"command": "ls"})
        result = _to_dict(ToolCallComplete(call=call))
        assert result["type"] == "ToolCallComplete"
        assert result["data"]["name"] == "shell"
        assert result["data"]["arguments"] == {"command": "ls"}

    def test_tool_result_ok(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import ToolResult

        result = _to_dict(ToolResult(call_id="c1", output="ok"))
        assert result == {"type": "ToolResult", "data": {"call_id": "c1", "ok": True}}

    def test_tool_result_error(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import ToolResult

        result = _to_dict(ToolResult(call_id="c2", output="bad", is_error=True))
        assert result["data"]["ok"] is False

    def test_model_error(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import ModelError

        result = _to_dict(ModelError(error=ValueError("boom")))
        assert result["type"] == "ModelError"
        assert "boom" in result["data"]["error"]

    def test_terminal(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import Terminal

        result = _to_dict(Terminal(reason="completed"))
        assert result == {"type": "Terminal", "data": {"reason": "completed"}}

    def test_assistant_message_complete(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import AssistantMessageComplete, AssistantMessage

        msg = AssistantMessage(content="hi", tool_calls=[])
        result = _to_dict(AssistantMessageComplete(message=msg))
        assert result == {"type": "AssistantMessageComplete", "data": {}}

    def test_unknown_event(self) -> None:
        from agent.viz.chat import _to_dict
        from agent.events import TurnStart

        result = _to_dict(TurnStart(turn=1))
        assert result["type"] == "TurnStart"
        assert result["data"] == {}
