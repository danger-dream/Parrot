import json
import unittest
from unittest.mock import patch

from src.openai.transform.stream_anthropic_to_responses import StreamTranslator


def decode(frames):
    return [json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames]


def send(translator, typ, **kwargs):
    data = {"type": typ, **kwargs}
    return decode(translator.feed((f"event: {typ}\ndata: {json.dumps(data)}\n\n").encode()))


def block(translator, index, kind, value="", stop=True):
    content = {"type": kind}
    if kind == "tool_use":
        content.update(id=f"call_{index}", name="test_tool", input={})
    events = send(translator, "content_block_start", index=index, content_block=content)
    delta = {
        "text": {"type": "text_delta", "text": value},
        "thinking": {"type": "thinking_delta", "thinking": value},
        "tool_use": {"type": "input_json_delta", "partial_json": value or "{}"},
    }[kind]
    events += send(translator, "content_block_delta", index=index, delta=delta)
    if stop:
        events += send(translator, "content_block_stop", index=index)
    return events


class StreamOrderTests(unittest.TestCase):
    def setUp(self):
        self.reasoning = patch(
            "src.openai.transform.stream_anthropic_to_responses.reasoning_passthrough_enabled",
            return_value=True,
        )
        self.reasoning.start()
        self.addCleanup(self.reasoning.stop)
        self.translator = StreamTranslator(model="test-model", created_ts=1)

    def assert_consistent(self, events, expected):
        done = [e for e in events if e["type"] == "response.output_item.done"]
        added = [e for e in events if e["type"] == "response.output_item.added"]
        final = events[-1]["response"]
        self.assertEqual([e["item"]["type"] for e in done], expected)
        self.assertEqual([e["output_index"] for e in done], list(range(len(expected))))
        self.assertEqual([e["output_index"] for e in added], list(range(len(expected))))
        self.assertEqual([e["item"] for e in done], final["output"])
        self.assertEqual(self.translator.get_downstream_responses_output(), final["output"])
        ids = [e["item"]["id"] for e in done]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([e["sequence_number"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(decode(self.translator.close()), [])
        return final

    def test_text_completes_at_block_stop(self):
        events = block(self.translator, 0, "text", "先说明，再调用工具。")
        self.assertEqual([e["type"] for e in events[-3:]], [
            "response.output_text.done", "response.content_part.done", "response.output_item.done",
        ])
        self.assertEqual(events[-1]["item"]["content"][0]["text"], "先说明，再调用工具。")
        self.assertEqual(send(self.translator, "content_block_stop", index=0), [])

    def test_thinking_text_multiple_tools(self):
        events = block(self.translator, 0, "thinking", "思考")
        events += block(self.translator, 1, "text", "说明")
        events += block(self.translator, 2, "tool_use", '{"a":1}')
        events += block(self.translator, 3, "tool_use", '{"b":2}')
        events += decode(self.translator.close())
        final = self.assert_consistent(events, ["reasoning", "message", "function_call", "function_call"])
        self.assertEqual(final["output_text"], "说明")
        self.assertEqual(final["output"][2]["arguments"], '{"a":1}')

    def test_text_tool_text_tool_text(self):
        events = []
        for idx, kind in enumerate(["text", "tool_use", "text", "tool_use", "text"]):
            events += block(self.translator, idx, kind, str(idx) if kind == "text" else "{}")
        events += decode(self.translator.close())
        final = self.assert_consistent(events, ["message", "function_call", "message", "function_call", "message"])
        self.assertEqual(final["output_text"], "024")
        self.assertEqual([i["content"][0]["text"] for i in final["output"] if i["type"] == "message"], ["0", "2", "4"])

    def test_consecutive_and_empty_text_blocks(self):
        events = block(self.translator, 0, "text", "A")
        events += block(self.translator, 1, "text", "")
        events += block(self.translator, 2, "text", "B")
        events += decode(self.translator.close())
        final = self.assert_consistent(events, ["message"] * 3)
        self.assertEqual(final["output_text"], "AB")

    def test_reasoning_disabled(self):
        self.translator._reasoning_enabled = False
        events = block(self.translator, 0, "thinking", "hidden")
        events += block(self.translator, 1, "text", "visible")
        events += block(self.translator, 2, "tool_use")
        events += decode(self.translator.close())
        self.assert_consistent(events, ["message", "function_call"])

    def test_single_text_tool_and_empty_stream(self):
        for kinds in [["text"], ["tool_use"], []]:
            with self.subTest(kinds=kinds):
                self.translator = StreamTranslator(model="test-model")
                events = []
                for idx, kind in enumerate(kinds):
                    events += block(self.translator, idx, kind, "hello" if kind == "text" else "{}")
                events += decode(self.translator.close())
                self.assert_consistent(events, ["message" if k == "text" else "function_call" for k in kinds])

    def test_close_finishes_unstopped_text_once(self):
        events = block(self.translator, 0, "text", "partial", stop=False)
        events += send(self.translator, "message_delta", delta={"stop_reason": "max_tokens"}, usage={"output_tokens": 3})
        events += decode(self.translator.close())
        final = self.assert_consistent(events, ["message"])
        self.assertEqual(final["status"], "incomplete")
        self.assertEqual(final["incomplete_details"], {"reason": "max_output_tokens"})

    def test_usage_and_store_output(self):
        self.translator._store_api_key_name = "test"
        self.translator._store_current_input = []
        events = send(self.translator, "message_start", message={"usage": {
            "input_tokens": 10, "cache_read_input_tokens": 4, "output_tokens": 1,
        }})
        events += block(self.translator, 0, "text", "before")
        events += block(self.translator, 1, "tool_use")
        events += block(self.translator, 2, "text", "after")
        events += send(self.translator, "message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 5})
        with patch("src.openai.store.is_enabled", return_value=True), patch("src.openai.store.save") as save:
            events += decode(self.translator.close())
        final = self.assert_consistent(events, ["message", "function_call", "message"])
        self.assertEqual(save.call_args.kwargs["output_items"], final["output"])
        self.assertEqual(final["usage"]["input_tokens"], 14)
        self.assertEqual(final["usage"]["output_tokens"], 5)

    def test_completed_events_replay_as_text_then_tool(self):
        from src.openai.transform.responses_to_anthropic import translate_request

        events = block(self.translator, 0, "text", "先说明")
        events += block(self.translator, 1, "tool_use", '{"a":1}')
        events += decode(self.translator.close())
        # Model a consumer that saves output items in completion order.
        history = [e["item"] for e in events if e["type"] == "response.output_item.done"]
        payload = translate_request({
            "model": "claude-test", "max_output_tokens": 100,
            "input": [{"role": "user", "content": "请调用工具"}] + history + [
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            ],
        }, store_enabled=False)
        assistant = [m for m in payload["messages"] if m["role"] == "assistant"]
        blocks = [b for m in assistant for b in m["content"]]
        self.assertEqual([b["type"] for b in blocks], ["text", "tool_use"])
        self.assertEqual(blocks[0]["text"], "先说明")
        self.assertEqual(payload["messages"][-1]["role"], "user")
        self.assertEqual(payload["messages"][-1]["content"][0]["type"], "tool_result")

    def test_byte_fragmented_stream(self):
        data = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "中文"}},
            {"type": "content_block_stop", "index": 0},
        ]
        raw = "".join(f"data: {json.dumps(d, ensure_ascii=False)}\n\n" for d in data).encode()
        events = []
        for byte in raw:
            events += decode(self.translator.feed(bytes([byte])))
        events += decode(self.translator.close())
        final = self.assert_consistent(events, ["message"])
        self.assertEqual(final["output_text"], "中文")


if __name__ == "__main__":
    unittest.main()
