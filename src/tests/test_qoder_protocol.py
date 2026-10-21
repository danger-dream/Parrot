"""Qoder wire protocol: body codec, COSY envelope, catalog, request body, failures and SSE decoding.

The vectors and the model listing come from the reference implementation
(magpie-community/plugins packages/qoder, MIT); nothing here contacts Qoder.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.oauth.qoder import catalog, request, signing
from src.providers import qoder_codec
from src.providers.qoder_codec import QoderStream, Splitter, failure

LISTING = json.loads((Path(__file__).parent / "fixtures" / "qoder" / "listing.json").read_text())
DEVICE = {"Cosy-MachineId": "m-123", "Cosy-MachineToken": "issued", "Cosy-MachineType": "type", "Cosy-MachineCode": "code", "Cosy-MachineOS": "x86_64_linux"}
ACCOUNT = {"uid": "u-1", "nickname": "张三", "email": "a@b.c", "access_token": "job-token", "machine_id": "m-123"}


@pytest.mark.parametrize("plain,wire", [
    ("", ""), ("a", "$p$#"), ("ab", "$SB#"), ("abc", "MSK#"), ("abcd", "$$KMD_#S"),
    ('{"中文":"测试 ✓","x":[1,2,3]}', "moByS^TrV*f^xxw$t#bnUQa&I)Y@B*f*mYGzhckOLnN*l*GO"),
])
def test_body_codec_matches_the_client(plain, wire):
    assert signing.encode_body(plain) == wire
    assert signing.decode_body(wire) == plain


def test_cosy_envelope_signs_the_wire_body_and_carries_the_account(monkeypatch):
    keys = []

    class Key:
        def encrypt(self, key, pad):
            keys.append(key)
            return b"wrapped:" + key

    monkeypatch.setattr(signing, "_RSA_KEY", Key())
    url = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1"
    headers = signing.cosy_headers(url, ACCOUNT, "WIRE", device=DEVICE, ts=1760000000)
    _, payload, signature = headers["Authorization"].removeprefix("Bearer ").split(".")
    assert headers["Authorization"].startswith("Bearer COSY.")
    assert signature == hashlib.md5(f"{payload}\n{headers['Cosy-Key']}\n1760000000\nWIRE\n/api/v2/service/pro/sse/agent_chat_generation".encode()).hexdigest()
    assert headers["Cosy-User"] == "u-1" and headers["Cosy-MachineId"] == "m-123" and headers["Cosy-Date"] == "1760000000"
    envelope = json.loads(base64.b64decode(payload))
    assert envelope["cosyVersion"] == "1.24.2" and envelope["version"] == "v1"
    key = keys[0]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    unpadder = sym_padding.PKCS7(128).unpadder()
    raw = decryptor.update(base64.b64decode(envelope["info"])) + decryptor.finalize()
    user = json.loads(unpadder.update(raw) + unpadder.finalize())
    assert user == {"uid": "u-1", "aid": "", "name": "张三", "email": "a@b.c", "security_oauth_token": "job-token"}


def test_cosy_envelope_refuses_an_account_without_machine_id():
    with pytest.raises(ValueError):
        signing.cosy_headers("https://api3.qoder.sh/algo/x", dict(ACCOUNT, machine_id=""), "", device=DEVICE)


def test_catalog_keeps_app_order_adds_chat_only_models_and_drops_routers():
    records = catalog.records_from_listing(LISTING)
    ids = [record["id"] for record in records]
    assert "auto" not in ids and "default" not in ids
    assert ids[:3] == ["ultimate", "performance", "fast"]  # chat-only performance follows ultimate there
    ultimate = records[0]
    assert ultimate["reasoningEfforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert ultimate["defaultReasoningEffort"] == "high" and ultimate["canDisableThinking"] is True
    assert ultimate["qoderWindows"] == [200000, 400000, 1000000] and ultimate["qoderDefaultWindow"] == 200000
    assert ultimate["contextWindow"] == 1000000 and ultimate["inputModalities"] == ["text", "image"]
    assert ultimate["qoderConfig"]["key"] == "ultimate" and ultimate["qoderRate"] == 2.0
    kimi = next(r for r in records if r["id"] == "kmodel_latest")
    assert kimi["reasoning"] is True and kimi["qoderAlwaysThinks"] is True and kimi["canDisableThinking"] is False
    flash = next(r for r in records if r["id"] == "qfmodel")
    assert flash["qoderFree"] is True and flash["qoderRate"] == 0 and flash["qoderRateWas"] == 0.1
    efficient = next(r for r in records if r["id"] == "efficient")
    assert efficient["qoderWindows"] == [] and efficient["contextWindow"] == 200000 and efficient["reasoning"] is False


RECORD = next(r for r in catalog.records_from_listing(LISTING) if r["id"] == "ultimate")
ALWAYS = next(r for r in catalog.records_from_listing(LISTING) if r["id"] == "kmodel_latest")
PLAIN = next(r for r in catalog.records_from_listing(LISTING) if r["id"] == "efficient")


@pytest.mark.parametrize("record,asked,expected", [
    (RECORD, None, (True, "high")), (RECORD, "none", (False, "")), (RECORD, "minimal", (True, "low")),
    (RECORD, "ultra", (True, "max")), (ALWAYS, "none", (True, "low")), (ALWAYS, "medium", (True, "high")),
    (ALWAYS, None, (True, "max")), (PLAIN, "high", (False, "")),
])
def test_effort_is_fitted_to_the_models_own_levels(record, asked, expected):
    assert request.effort_for(asked, record) == expected


def test_window_grows_only_when_the_request_needs_it():
    small = {"messages": [{"role": "user", "content": "hi"}]}
    assert request.window_for(small, RECORD, 32000) == 200000
    big = {"messages": [{"role": "user", "content": "x" * 600000}]}
    assert request.window_for(big, RECORD, 32000) == 400000
    huge = {"messages": [{"role": "user", "content": "x" * 5000000}]}
    assert request.window_for(huge, RECORD, 0) == 1000000
    assert request.window_for(small, PLAIN, 0) == 200000


def test_body_writes_system_tools_tool_results_and_images():
    payload = {
        "model": "ultimate", "max_tokens": 1000, "reasoning_effort": "low", "tool_choice": "required",
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}],
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": {"p": 1}}}]},
            {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "ok"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,BB"}}]},
            {"role": "user", "content": "and?"},
        ],
    }
    body = request.body_for(payload, RECORD)
    assert body["parameters"] == {"enable_thinking": True, "max_tokens": 1000, "reasoning_effort": "low", "context_length": 200000}
    assert body["system"][0]["text"].startswith(request.QODER_SYSTEM + "\n\nbe brief\nYou must call")
    assert body["model_config"]["key"] == "ultimate" and body["session_type"] == "app"
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    assert messages[2]["tool_calls"][0]["function"] == {"name": "read", "arguments": '{"p":1}'}
    assert messages[3] == {"role": "tool", "tool_call_id": "c1",
                           "content": "ok\n\n[The tool returned an image; it follows in the next message.]"}
    assert messages[4]["content"][0] == {"type": "text", "text": "[From the result of read (tool call c1):]"}
    assert messages[4]["content"][-1] == {"type": "text", "text": "and?"}
    assert body["tools"][0]["function"]["name"] == "read"
    assert "tools" not in request.body_for(dict(payload, tool_choice="none"), RECORD)


@pytest.mark.parametrize("status,text,expected", [
    (403, '{"code":"10605","message":"{\\"isQueued\\":false,\\"modelKey\\":\\"efficient\\",\\"retryAfterSeconds\\":12,\\"serviceAvailable\\":false}"}', (503, "10605", 12)),
    (403, '\ufeff{"code":"10605","message":"busy"}', (503, "10605", 30)),
    (401, '{"code":"401","message":"{\\"code\\":\\"10605\\",\\"message\\":\\"queued\\"}"}', (503, "10605", 30)),
    (403, '{"code":"112","message":"no credits"}', (429, "112", None)),
    (400, '{"code":"406","message":"sensitive"}', (400, "406", None)),
    (401, '{"code":"105","message":"loginExpired"}', (401, "105", None)),
    (401, '{"code":"401","message":"token invalid"}', (401, "401", None)),
    (401, '{"code":"1001","message":"other"}', (403, "1001", None)),
    (403, '{"code":"1001","message":"Login Expired"}', (401, "1001", None)),
    (403, "", (403, "", None)),
    (403, '{"code":"777","message":"other"}', (403, "777", None)),
    (500, "oops", (500, "500", None)),
])
def test_failure_reads_qoders_own_code(status, text, expected):
    mapped = failure(status, text)
    assert (mapped["status"], mapped["error"]["code"], mapped.get("retry_after")) == expected
    if mapped["status"] == 400:
        assert mapped["error"]["type"] == "invalid_request_error"


def frame(body) -> bytes:
    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return b"data:" + json.dumps({"headers": {}, "body": raw, "statusCodeValue": 200, "statusCode": "OK"}, ensure_ascii=False).encode() + b"\n\n"


def chunk(delta, **extra) -> bytes:
    return frame({"choices": [{"delta": delta, "index": 0, **extra}], "object": "chat.completion.chunk"})


def pieces(content: str, size: int = 3) -> bytes:
    return b"".join(chunk({"content": content[i:i + size]}) for i in range(0, len(content), size))


def decode(raw: bytes, *, cut: int = 7) -> dict:
    stream = QoderStream("ultimate")
    out = b"".join(stream.feed(raw[i:i + cut]) for i in range(0, len(raw), cut)) + stream.finish()
    frames = [json.loads(line[6:]) for line in out.decode().split("\n") if line.startswith("data: ") and line != "data: [DONE]"]
    deltas = [f["choices"][0]["delta"] for f in frames if f.get("choices")]
    return {
        "text": "".join(d.get("content") or "" for d in deltas),
        "reasoning": "".join(d.get("reasoning_content") or "" for d in deltas),
        "calls": [(t["function"]["name"], json.loads(t["function"]["arguments"] or "{}")) for d in deltas for t in d.get("tool_calls") or [] if "name" in t["function"]],
        "finish": [f["choices"][0]["finish_reason"] for f in frames if f.get("choices") and f["choices"][0]["finish_reason"]],
        "errors": [f["error"] for f in frames if "error" in f],
        "usage": next((f["usage"] for f in frames if f.get("usage")), None),
        "done": out.endswith(b"data: [DONE]\n\n"),
    }


def test_stream_reads_usage_after_the_finish_and_a_bom_led_body():
    raw = (chunk({"content": "Hi", "reasoning_content": "think"}) + chunk({"content": ""}, finish_reason="stop")
           + frame({"choices": [], "usage": {"completion_tokens": 106, "completion_tokens_details": {"reasoning_tokens": 94},
                                              "prompt_tokens": 40, "prompt_tokens_details": {"cached_tokens": 32}}})
           + frame("\ufeff" + json.dumps({"choices": [{"delta": {"content": "!"}, "index": 0}]}))
           + frame("[DONE]") + b'data:{"firstTokenDuration":821}\n\n')
    out = decode(raw)
    assert out["text"] == "Hi!" and out["reasoning"] == "think" and out["finish"] == ["stop"] and out["done"]
    assert out["usage"] == {"prompt_tokens": 40, "completion_tokens": 106, "total_tokens": 146,
                            "prompt_tokens_details": {"cached_tokens": 32}, "completion_tokens_details": {"reasoning_tokens": 94}}


READABLE = 'Reading.\n<tool_call>{"name":"read","arguments":{"path":"a.go"}}</tool_call>'


@pytest.mark.parametrize("raw", [chunk({"content": READABLE}) + chunk({}, finish_reason="tool_calls"), pieces(READABLE) + frame("[DONE]")])
def test_a_tool_call_written_as_text_becomes_the_call(raw):
    out = decode(raw)
    assert out["text"] == "Reading.\n" and out["calls"] == [("read", {"path": "a.go"})]
    assert out["finish"] == ["tool_calls"] and out["errors"] == []


@pytest.mark.parametrize("content,text", [
    ('Sure.\n<tool_call>{"name":"read","arguments":{"path":}}</tool_call>', "Sure.\n"),
    ('Reading.\n<tool_call>{"name":"read","arguments":{"path":"a.go"', "Reading.\n"),
    ("Reading.\n<tool_call><function=read><parameter=path>a.go", "Reading.\n"),
    ("Reading.\n<tool_call><function=><parameter=path>a.go</parameter></function></tool_call>", "Reading.\n"),
])
def test_an_unreadable_or_cut_short_call_ends_the_turn_as_an_error(content, text):
    for raw in (chunk({"content": content}), pieces(content)):
        out = decode(raw)
        assert out["text"] == text and "<tool_call>" not in out["text"]
        assert out["calls"] == [] and out["finish"] == [] and len(out["errors"]) == 1 and not out["done"]


def test_a_whole_call_without_its_close_and_the_xml_form_are_calls():
    assert decode(chunk({"content": 'Reading.\n<tool_call>{"name":"read","arguments":{"path":"a.go"}}'}) + frame("[DONE]"))["calls"] == [("read", {"path": "a.go"})]
    xml = decode(chunk({"content": "<tool_call><function=read><parameter=path>a.go</parameter><parameter=n>3</parameter></function>"}) + frame("[DONE]"))
    assert xml["calls"] == [("read", {"path": "a.go", "n": 3})] and xml["finish"] == ["tool_calls"]


@pytest.mark.parametrize("content", [
    "Use <tool_call>incomplete",
    "A block is <tool_call>name</tool_call> in prose.",
    'Write it as `<tool_call>{"name":` then the rest.',
    'An example:\n```\n<tool_call>{"name":"read","arguments":{"path":}}</tool_call>\n```\n',
    "Ends on <tool_c",
])
def test_text_that_only_looks_like_a_tag_stays_text(content):
    out = decode(chunk({"content": content}) + frame("[DONE]"))
    assert out["text"] == content and out["finish"] == ["stop"] and out["errors"] == []


def test_native_tool_calls_keep_qoders_id_and_stream_arguments():
    raw = (chunk({"content": "x"}) + chunk({"tool_calls": [{"index": 0, "id": "call-q", "function": {"name": "read", "arguments": '{"p":'}}]})
           + chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}) + frame("[DONE]"))
    stream = QoderStream("ultimate")
    out = (stream.feed(raw) + stream.finish()).decode()
    assert '"id":"call-q"' in out and '"arguments":"\\"a\\"}"' in out and '"finish_reason":"tool_calls"' in out


def test_an_error_envelope_and_an_empty_answer_are_errors():
    queued = json.dumps({"code": "10605", "message": json.dumps({"isQueued": True, "retryAfterSeconds": 7})})
    error = b"data:" + json.dumps({"body": queued, "statusCodeValue": 403}).encode() + b"\n\n"
    out = decode(error)
    assert out["errors"][0]["code"] == "10605" and out["errors"][0]["retry_after"] == 7
    assert out["text"] == "" and not out["done"]
    # An error envelope with no body is read from the envelope itself.
    bare = b"data:" + json.dumps({"code": "112", "message": "credits", "statusCodeValue": 403}).encode() + b"\n\n"
    assert decode(bare)["errors"][0]["code"] == "112"
    empty = decode(frame("[DONE]"))
    assert empty["errors"][0]["code"] == "qoder_empty_answer"


def test_a_sign_in_refused_inside_a_200_envelope_is_an_authentication_error():
    for raw in (frame({"code": "105", "message": "loginExpired"}), frame({"code": 401, "message": "login expired"})):
        out = decode(raw + frame("[DONE]"))
        assert out["errors"][0]["type"] == "authentication_error" and not out["done"]


def test_a_bom_led_line_and_odd_usage_numbers_are_still_read():
    raw = (b"\xef\xbb\xbf" + chunk({"content": "Hi"}) + chunk({"content": "!"}, finish_reason="stop")
           + frame({"choices": [], "usage": {"prompt_tokens": "40", "completion_tokens": "n/a"}}) + frame("[DONE]"))
    out = decode(raw)
    assert out["text"] == "Hi!" and out["usage"]["prompt_tokens"] == 40 and out["usage"]["completion_tokens"] == 0


def test_splitter_never_releases_a_partial_open_tag():
    splitter = Splitter()
    assert splitter.feed("abc<tool") == [("text", "abc")]
    assert splitter.feed("_cal") == []
    assert splitter.feed("l>{") == []
    assert splitter.flush() == [("error", qoder_codec.CUT_SHORT)]
