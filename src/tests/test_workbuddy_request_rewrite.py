"""#30: scoped/configurable templates; real channel wire and hot config reload."""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

import httpx
import pytest

from src import config
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel
from src.openai.channel.api_channel import OpenAIApiChannel
from src.tests.test_workbuddy_channel import env, request_body, call, frames
from src.tests import test_protocol_fake_upstreams as fake
from src.workbuddy_request_rewrite import (
    DEFAULT_REQUEST_REWRITE, RewriteConfigError, rewrite_payload, settings_from_config, validate_settings,
)

IDENTITY = DEFAULT_REQUEST_REWRITE["rules"][0]["match"]
ENV = DEFAULT_REQUEST_REWRITE["rules"][1]["match"]
NEUTRAL = DEFAULT_REQUEST_REWRITE["rules"][0]["replace"]
ENV_SAFE = DEFAULT_REQUEST_REWRITE["rules"][1]["replace"]
SYSTEM = f"{IDENTITY}\n<env>\nCurrent branch: master\n{ENV}: main\n</env>"
SAFE_SYSTEM = SYSTEM.replace(IDENTITY, NEUTRAL).replace(ENV, ENV_SAFE)


def payload(text=SYSTEM):
    return {"messages": [{"role": "system", "content": text}]}


def test_default_rewrite_is_copy_on_write_and_idempotent():
    body = payload()
    for role in ("developer", "user", "assistant", "tool"):
        body["messages"].append({"role": role, "content": SYSTEM})
    body["tools"] = [{"type": "function", "function": {"name": "literal", "description": SYSTEM,
        "parameters": {"type": "string", "enum": [IDENTITY, ENV]}}}]
    body["messages"][3]["tool_calls"] = [{"id": "same-id", "type": "function",
        "function": {"name": "literal", "arguments": json.dumps({"value": SYSTEM})}}]
    before = copy.deepcopy(body)
    changed, hits = rewrite_payload(body, DEFAULT_REQUEST_REWRITE)
    assert body == before
    assert changed["messages"][0]["content"] == SAFE_SYSTEM
    assert changed["messages"][1:] == body["messages"][1:]
    assert changed["tools"] == body["tools"]
    assert hits == {"cc-identity": 1, "cc-env-main-branch": 1}
    assert rewrite_payload(changed, DEFAULT_REQUEST_REWRITE) == (changed, {})


def test_typed_parts_preserve_boundaries_metadata_whitespace_and_crlf():
    body = {"messages": [{"role": "system", "name": "unchanged", "content": [
        {"type": "text", "text": "\r\n  " + IDENTITY + "  \r\n", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "<env>\r\n"},
        {"type": "text", "text": "\t" + ENV + ": release/测试  \r\n</env>"},
    ]}]}
    before = copy.deepcopy(body)
    changed, hits = rewrite_payload(body, DEFAULT_REQUEST_REWRITE)
    assert body == before and len(changed["messages"][0]["content"]) == 3
    parts = changed["messages"][0]["content"]
    assert parts[0] == dict(before["messages"][0]["content"][0], text="\r\n  " + NEUTRAL + "  \r\n")
    assert parts[1] == before["messages"][0]["content"][1]
    assert parts[2]["text"] == "\t" + ENV_SAFE + ": release/测试  \r\n</env>"
    assert hits == {"cc-identity": 1, "cc-env-main-branch": 1}


@pytest.mark.parametrize("text", [
    "Please quote exactly: " + IDENTITY,
    "Reference material:\n" + IDENTITY,
    "```text\n" + SYSTEM + "\n```",
    "~~~\n" + SYSTEM + "\n~~~",
    "<example>\n" + SYSTEM + "\n</example>",
    "<quote>\n<env>\n" + ENV + ": main\n</env>\n</quote>",
    "<env>\n" + ENV + ": main",  # no complete block
    "<env>\n" + ENV + ": main\n</wrong>",
    "<env quoted='true'>\n" + ENV + ": main\n</env>",
    "<env>\n```\n" + ENV + ": main\n```\n</env>",
    "<env>\n> " + ENV + ": main\n</env>",
    "<env>\nDo not change " + ENV + ": main\n</env>",
    "<env>\n" + ENV + " extra: main\n</env>",
    IDENTITY[:-1],  # not a fuzzy identity/brand match
])
def test_quotes_partial_templates_and_nonmatching_lines_are_untouched(text):
    body = payload(text)
    assert rewrite_payload(body, DEFAULT_REQUEST_REWRITE) == (body, {})


def test_unknown_system_content_parts_are_not_searched_or_bridged():
    body = {"messages": [{"role": "system", "content": [
        {"type": "text", "text": SYSTEM}, {"type": "image_url", "image_url": {"url": SYSTEM}},
    ]}]}
    assert rewrite_payload(body, DEFAULT_REQUEST_REWRITE) == (body, {})


def test_configurable_rules_are_literal_ordered_non_cascading_and_disableable():
    rule = {"id": "new-template", "scope": "system_env_line", "match": "Future template (v2)", "replace": "Future label"}
    other = dict(rule, id="second", match="Future label", replace="MUST NOT CASCADE")
    settings = {"enabled": True, "rules": [rule, other]}
    body = payload("<env>\nFuture template (v2): main\n</env>")
    changed, hits = rewrite_payload(body, settings)
    assert changed["messages"][0]["content"] == "<env>\nFuture label: main\n</env>"
    assert hits == {"new-template": 1}
    assert rewrite_payload(body, dict(settings, enabled=False)) == (body, {})
    assert rewrite_payload(body, dict(settings, rules=[])) == (body, {})
    assert rewrite_payload(body, dict(settings, rules=[dict(rule, enabled=False)])) == (body, {})
    # Regex metacharacters are literal, and a failed prefix is not a match.
    assert rewrite_payload(payload("<env>\nFuture template v2: main\n</env>"), settings)[1] == {}


def bad_settings():
    rule = copy.deepcopy(DEFAULT_REQUEST_REWRITE["rules"][0])
    return [None, [], {"enabled": "true"}, {"rules": "bad"}, {"unknown": True},
        {"rules": [None]}, {"rules": [dict(rule, scope="whole_body")]},
        {"rules": [dict(rule, id="bad\nsecret")]}, {"rules": [rule, rule]},
        {"rules": [dict(rule, match="")]}, {"rules": [dict(rule, match="x\ny")]},
        {"rules": [dict(rule, replace="x\ry")]}, {"rules": [dict(rule, replace="x\u2028y")]},
        {"rules": [dict(rule, enabled=1)]}, {"rules": [dict(rule, regex=True)]},
        {"rules": [dict(rule, id=f"rule{i}") for i in range(65)]},
        {"rules": [dict(rule, match="x" * 2049)]}]


@pytest.mark.parametrize("settings", bad_settings())
def test_bad_rule_config_fails_without_echoing_values(settings):
    with pytest.raises(RewriteConfigError, match="workbuddy.requestRewrite") as exc:
        validate_settings(settings)
    assert "secret" not in str(exc.value) and IDENTITY not in str(exc.value)


def _set_system(body, ingress, typed):
    if ingress == "anthropic":
        body["system"] = [{"type": "text", "text": SYSTEM}] if typed else SYSTEM
    elif ingress == "responses":
        body["instructions"] = [{"role": "system", "content": [{"type": "input_text", "text": SYSTEM}]}] if typed else SYSTEM
    else:
        body["messages"].insert(0, {"role": "system", "content": [{"type": "text", "text": SYSTEM}] if typed else SYSTEM})


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("typed", [False, True])
async def test_real_handler_serializes_templates_after_translation_without_mutating_input(env, ingress, stream, typed, caplog):
    body = request_body(ingress, stream, tool=True)
    _set_system(body, ingress, typed)
    before = copy.deepcopy(body)
    def wire(req):
        emitted = json.loads(req.content)
        systems = json.dumps([m for m in emitted["messages"] if m["role"] == "system"], ensure_ascii=False)
        assert IDENTITY not in systems and ENV not in systems
        assert NEUTRAL in systems and ENV_SAFE in systems
        assert emitted["messages"][0] == {"role": "system", "content": "You are CodeBuddy Code."}
        assert emitted["tools"] and emitted["tool_choice"] == "fixture_tool"
        return httpx.Response(200, stream=fake.ChunkedByteStream(frames(tool=True)), headers={"content-type": "text/event-stream"})
    with caplog.at_level(logging.DEBUG, logger="src.channel.workbuddy_oauth_channel"):
        response, _, requests = await call(env, ingress, body, wire)
    assert response.status_code == 200 and len(requests) == 1
    # The handlers add internal request metadata; the caller-owned business body
    # is preserved exactly, and a later candidate sees the unmodified templates.
    assert {k:v for k,v in body.items() if not k.startswith("_parrot") and k != "_codex_turn_serialization_required"} == before
    logs = [r.message for r in caplog.records if r.name == "src.channel.workbuddy_oauth_channel"]
    assert any("rule=cc-identity hits=1" in line for line in logs)
    assert all(IDENTITY not in line and ENV not in line and "master" not in line for line in logs)


@pytest.mark.parametrize("profile", ["cli", "ide"])
async def test_global_profile_is_not_rewritten(env, profile):
    _, old = env
    entry = dict(old.account, realm="global", domain="www.codebuddy.ai", workbuddy_client_profile=profile)
    config.update(lambda c: c.update(oauthAccounts=[entry]))
    ch = WorkBuddyOAuthChannel(entry)
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "ping"}]}
    outgoing = json.loads((await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")).body)
    assert outgoing["messages"][1]["content"] == SYSTEM


async def test_other_provider_and_business_strings_keep_original_request(env):
    _, wb = env
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": SYSTEM}],
        "tools": [{"type": "function", "function": {"name": "exact", "parameters": {"type": "string", "enum": [ENV, IDENTITY]}}}]}
    before = copy.deepcopy(body)
    outgoing = json.loads((await wb.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")).body)
    assert body == before and outgoing["messages"][-1]["content"] == SYSTEM
    assert outgoing["tools"] == body["tools"]
    other = OpenAIApiChannel({"name": "unaffected", "baseUrl": "https://other.example.test", "apiKey": "fixture-only",
        "protocol": "openai-chat", "models": [{"real": "glm-fixture", "alias": "glm-fixture"}]})
    other_body = json.loads((await other.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")).body)
    assert other_body["messages"] == body["messages"] and other_body["tools"] == body["tools"]


async def test_same_channel_next_request_observes_file_edits_and_custom_rules(env):
    _, ch = env
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "ping"}]}
    async def emitted():
        return json.loads((await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")).body)["messages"][1]["content"]
    def edit(mutator):
        path = Path(config.path())
        cfg = json.loads(path.read_text())
        mutator(cfg["workbuddy"]["requestRewrite"])
        old_mtime = path.stat().st_mtime
        path.write_text(json.dumps(cfg))
        os.utime(path, (old_mtime + 2, old_mtime + 2))
    assert await emitted() == SAFE_SYSTEM
    edit(lambda s: s.update(enabled=False))
    assert await emitted() == SYSTEM
    edit(lambda s: s.update(enabled=True, rules=[]))
    assert await emitted() == SYSTEM
    edit(lambda s: s.update(rules=[{"id": "custom-identity", "scope": "system_prefix_line", "match": IDENTITY, "replace": "Custom configured identity."}]))
    assert await emitted() == SYSTEM.replace(IDENTITY, "Custom configured identity.")
    assert body["messages"][0]["content"] == SYSTEM


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    from src.tests.test_config_atomic_update import _install_private_config
    return _install_private_config(config, tmp_path, monkeypatch)


def test_invalid_save_is_atomic_and_invalid_manual_hotload_keeps_last_valid_snapshot(private_config, capsys):
    path, _ = private_config
    old = config.get()
    before = path.read_bytes()
    callbacks = []
    config.on_reload(lambda cfg: callbacks.append(cfg))
    with pytest.raises(RewriteConfigError):
        config.update(lambda c: c["workbuddy"]["requestRewrite"].update(rules=[{"match": "private-rule-text"}]))
    assert config.get() is old and path.read_bytes() == before and not callbacks
    bad = copy.deepcopy(old)
    bad["workbuddy"]["requestRewrite"]["rules"][0]["scope"] = "whole_body"
    path.write_text(json.dumps(bad))
    os.utime(path, (config._mtime + 2, config._mtime + 2))
    invalid_bytes = path.read_bytes()
    assert config.get() is old and config.get() is old and not callbacks
    assert path.read_bytes() == invalid_bytes
    output = capsys.readouterr().out
    assert output.count("retaining last valid config") == 1 and "private-rule-text" not in output
    with pytest.raises(RewriteConfigError):
        config.reload()
    bad["workbuddy"]["requestRewrite"] = {"enabled": False, "rules": []}
    path.write_text(json.dumps(bad))
    os.utime(path, (config._mtime + 2, config._mtime + 2))
    assert config.get()["workbuddy"]["requestRewrite"] == {"enabled": False, "rules": []}
    assert len(callbacks) == 1


def test_first_load_invalid_rules_is_explicit_and_does_not_rewrite_file(private_config, monkeypatch):
    path, old = private_config
    old["workbuddy"]["requestRewrite"]["rules"] = "invalid"
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    monkeypatch.setattr(config, "_cache", None)
    with pytest.raises(RewriteConfigError):
        config.get()
    assert path.read_bytes() == before


def test_defaults_backfill_but_explicit_rule_lists_replace_not_merge(private_config):
    path, old = private_config
    old.pop("workbuddy")
    path.write_text(json.dumps(old))
    loaded = config.reload()
    assert loaded["workbuddy"]["requestRewrite"] == DEFAULT_REQUEST_REWRITE
    assert json.loads(path.read_text())["workbuddy"]["requestRewrite"] == DEFAULT_REQUEST_REWRITE
    custom = {"enabled": True, "rules": [{"id": "only", "scope": "system_prefix_line", "match": "Old", "replace": "New"}]}
    loaded["workbuddy"]["requestRewrite"] = custom
    path.write_text(json.dumps(loaded))
    assert config.reload()["workbuddy"]["requestRewrite"] == custom
    loaded["workbuddy"]["requestRewrite"]["rules"] = []
    path.write_text(json.dumps(loaded))
    assert config.reload()["workbuddy"]["requestRewrite"]["rules"] == []


def test_example_defaults_are_in_sync_and_settings_accessor_is_pure():
    example = json.loads((Path(__file__).resolve().parents[2] / "config.example.json").read_text())
    assert example["workbuddy"]["requestRewrite"] == config.DEFAULT_CONFIG["workbuddy"]["requestRewrite"] == DEFAULT_REQUEST_REWRITE
    empty = {}
    assert settings_from_config(empty) == DEFAULT_REQUEST_REWRITE and empty == {}
    with pytest.raises(RewriteConfigError):
        settings_from_config({"workbuddy": None})
