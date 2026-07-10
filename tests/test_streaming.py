"""Unit tests — SSE stream accumulators (no network).

Run: .venv/bin/python tests/test_streaming.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class FakeStream:
    """Minimal httpx.Response stand-in: only aiter_lines()."""

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def sse(obj):
    return ["data: " + json.dumps(obj), ""]


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_anthropic_text_thinking_tooluse():
    lines = []
    lines += sse({"type": "message_start", "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 40}}})
    lines += sse({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    lines += sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "let me "}})
    lines += sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "think"}})
    lines += sse({"type": "content_block_stop", "index": 0})
    lines += sse({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
    lines += sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hola "}})
    lines += sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "mundo"}})
    lines += sse({"type": "content_block_stop", "index": 1})
    lines += sse({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}}})
    lines += sse({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path": "a'}})
    lines += sse({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '.py"}'}})
    lines += sse({"type": "content_block_stop", "index": 2})
    lines += sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}})
    lines += sse({"type": "message_stop"})

    out = run(server._consume_anthropic_stream(FakeStream(lines)))
    assert out["stop_reason"] == "tool_use", out
    assert out["usage"]["input_tokens"] == 100
    assert out["usage"]["cache_read_input_tokens"] == 40
    assert out["usage"]["output_tokens"] == 42
    assert out["content"][0] == {"type": "thinking", "thinking": "let me think"}
    assert out["content"][1] == {"type": "text", "text": "hola mundo"}
    tu = out["content"][2]
    assert tu["type"] == "tool_use" and tu["name"] == "read_file"
    assert tu["input"] == {"path": "a.py"}
    print("PASS anthropic text+thinking+tool_use")


def test_anthropic_error_event():
    lines = sse({"type": "message_start", "message": {"usage": {}}})
    lines += sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    try:
        run(server._consume_anthropic_stream(FakeStream(lines)))
        raise AssertionError("expected BackendStreamError")
    except server.BackendStreamError as e:
        assert "Overloaded" in str(e)
    print("PASS anthropic error event -> BackendStreamError")


def test_anthropic_malformed_tool_json():
    lines = sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "f", "input": {}}})
    lines += sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"broken": '}})
    lines += sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}})
    out = run(server._consume_anthropic_stream(FakeStream(lines)))
    assert out["content"][0]["input"] == {}
    print("PASS anthropic malformed tool json -> {}")


def test_openai_text_reasoning_usage():
    lines = []
    lines += sse({"choices": [{"delta": {"reasoning_content": "hm"}}]})
    lines += sse({"choices": [{"delta": {"reasoning_content": "mm"}}]})
    lines += sse({"choices": [{"delta": {"content": "respuesta "}}]})
    lines += sse({"choices": [{"delta": {"content": "final"}, "finish_reason": None}]})
    lines += sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    lines += sse({"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 20}}})
    lines += ["data: [DONE]", ""]

    openai_resp = run(server._consume_openai_stream(FakeStream(lines)))
    out = server._openai_to_anthropic_response(openai_resp)
    assert out["stop_reason"] == "end_turn", out
    assert out["usage"] == {"input_tokens": 30, "output_tokens": 9, "cache_read_input_tokens": 20}
    types = [b["type"] for b in out["content"]]
    assert types == ["thinking", "text"], types
    assert out["content"][0]["thinking"] == "hmmm"
    assert out["content"][1]["text"] == "respuesta final"
    print("PASS openai text+reasoning+usage")


def test_openai_tool_call_fragments():
    lines = []
    lines += sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "run_bash", "arguments": ""}}]}}]})
    lines += sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"command": "ls'}}]}}]})
    lines += sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' -la"}'}}]}}]})
    lines += sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    lines += ["data: [DONE]", ""]

    openai_resp = run(server._consume_openai_stream(FakeStream(lines)))
    out = server._openai_to_anthropic_response(openai_resp)
    assert out["stop_reason"] == "tool_use"
    tu = out["content"][0]
    assert tu["type"] == "tool_use" and tu["name"] == "run_bash" and tu["id"] == "call_1"
    assert tu["input"] == {"command": "ls -la"}
    print("PASS openai tool_call fragments")


def test_openai_error_event():
    lines = sse({"error": {"type": "server_error", "message": "boom"}})
    try:
        run(server._consume_openai_stream(FakeStream(lines)))
        raise AssertionError("expected BackendStreamError")
    except server.BackendStreamError as e:
        assert "boom" in str(e)
    print("PASS openai error event -> BackendStreamError")


def test_sse_multiline_and_junk():
    lines = [
        ": comment, ignore",
        "event: content_block_delta",
        "data: {\"type\": \"content_block_start\", \"index\": 0,",
    ]
    # multiline data (2 data: lines = joined with \n → invalid JSON, skipped)
    lines += ["data: garbage", ""]
    lines += sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "ok"}})
    lines += sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})
    out = run(server._consume_anthropic_stream(FakeStream(lines)))
    assert out["content"] == [{"type": "text", "text": "ok"}]
    assert out["stop_reason"] == "end_turn"
    print("PASS sse junk/comments tolerated")


if __name__ == "__main__":
    test_anthropic_text_thinking_tooluse()
    test_anthropic_error_event()
    test_anthropic_malformed_tool_json()
    test_openai_text_reasoning_usage()
    test_openai_tool_call_fragments()
    test_openai_error_event()
    test_sse_multiline_and_junk()
    print("\nALL PASS (7/7)")
