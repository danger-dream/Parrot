"""Gemini JSON Schema must not pass through the legacy protobuf cleaner."""
import copy
import json

import pytest

from src import config, oauth_manager
from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
from src.providers import antigravity_codec as codec


SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {"type": ["array", "object"], "items": {
            "type": "object", "properties": {"status": {"enum": ["passed", "completed"]}},
            "required": ["status"],
        }},
        "blocker": {"type": ["object", "string"]},
        "nullable": {"type": ["string", "null"]},
        "nested": {"anyOf": [{"$ref": "#/$defs/word"}, {"type": "null"}]},
        "choice": {"oneOf": [{"const": "x"}, {"const": "y"}]},
        "combined": {"allOf": [{"type": "integer"}, {"minimum": 0, "maximum": 9}]},
        "type": {"type": ["boolean", "null"]},
    },
    "$defs": {"word": {"type": "string", "minLength": 1}},
    "required": ["criteria", "blocker"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("model", ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.1-pro-high"])
def test_gemini_full_schema_survives_both_conversion_passes(model):
    arguments = {"type": ["ordinary", "data"], "title": "unchanged", "const": 7, "anyOf": [1, 2]}
    payload = {"model": model, "input": [
        {"role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "call-1", "name": "SchemaProbe", "arguments": json.dumps(arguments)},
    ], "tools": [{"type": "function", "name": "SchemaProbe", "parameters": copy.deepcopy(SCHEMA)}],
        "text": {"format": {"type": "json_schema", "name": "UnionReply", "schema": copy.deepcopy(SCHEMA)}}}
    original = copy.deepcopy(payload)
    converted = codec.responses_to_gemini(payload)
    wire = codec.wrap_cloud_code(converted, model=model, project_id="synthetic-project")["request"]
    declaration = wire["tools"][0]["functionDeclarations"][0]
    assert declaration["parametersJsonSchema"] == SCHEMA
    assert "parameters" not in declaration
    assert wire["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert "responseSchema" not in wire["generationConfig"]
    assert wire["contents"][1]["parts"][0]["functionCall"]["args"] == arguments
    assert payload == original
    declaration["parametersJsonSchema"]["properties"]["blocker"]["type"].append("null")
    assert payload == original  # wire mutations cannot alter caller schemas


@pytest.mark.parametrize("snake", [False, True])
def test_explicit_json_schema_fields_bypass_cleaner_in_wrapper(snake):
    parameter_key = "parameters_json_schema" if snake else "parametersJsonSchema"
    response_key = "response_json_schema" if snake else "responseJsonSchema"
    request = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
               "tools": [{"functionDeclarations": [{"name": "SchemaProbe", parameter_key: copy.deepcopy(SCHEMA)}]}],
               "generationConfig": {response_key: copy.deepcopy(SCHEMA)}}
    wire = codec.wrap_cloud_code(request, model="gemini-3.8-flash-high", project_id="synthetic-project")["request"]
    assert wire["tools"][0]["functionDeclarations"][0][parameter_key] == SCHEMA
    assert wire["generationConfig"][response_key] == SCHEMA


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-opus-4-6-thinking"])
def test_claude_legacy_schema_and_placeholder_remain_unchanged(model):
    payload = {"model": model, "input": "hi", "tools": [
        {"type": "function", "name": "SchemaProbe", "parameters": {"type": "object"}},
    ]}
    wire = codec.wrap_cloud_code(codec.responses_to_gemini(payload), model=model, project_id="synthetic-project")["request"]
    declaration = wire["tools"][0]["functionDeclarations"][0]
    assert "parametersJsonSchema" not in declaration
    assert declaration["parameters"]["required"] == ["reason"]
    assert declaration["parameters"]["properties"]["reason"]["type"] == "string"
    assert wire["toolConfig"]["functionCallingConfig"]["mode"] == "VALIDATED"


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_real_channel_keeps_schema_across_each_ingress(protocol, stream, monkeypatch):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    monkeypatch.setattr(config, "get", lambda: cfg)
    async def token(_):
        return "synthetic-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    model = "gemini-3.8-flash-high"
    channel = AntigravityOAuthChannel({"email": "schema@example.test", "project_id": "synthetic-project", "models": [model]})
    common = {"model": model, "stream": stream}
    function = {"name": "SchemaProbe", "parameters": copy.deepcopy(SCHEMA)}
    if protocol == "responses":
        body = {**common, "input": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", **function}]}
    elif protocol == "chat":
        body = {**common, "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": function}]}
    else:
        body = {**common, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "SchemaProbe", "input_schema": copy.deepcopy(SCHEMA)}]}
    original = copy.deepcopy(body)
    outbound = await channel.build_upstream_request(body, model, ingress_protocol=protocol)
    wire = json.loads(outbound.body)["request"]
    declaration = wire["tools"][0]["functionDeclarations"][0]
    assert declaration["parametersJsonSchema"] == SCHEMA
    assert "parameters" not in declaration
    assert body == original
