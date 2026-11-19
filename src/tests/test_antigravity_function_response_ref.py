"""Regression tests for Antigravity functionResponse $ref wrapping and RPM fallback window."""
from __future__ import annotations

import copy
import json
import time

from src.providers import antigravity_codec as ac
from src.tests.test_antigravity_oauth import _add_ag_account, _import_modules, _setup


def test_responses_to_gemini_wraps_json_schema_ref_without_altering_normal_json():
    schema_out = json.dumps({
        "action": "create",
        "configSchema": {
            "$defs": {"Retry": {"type": "object"}},
            "properties": {
                "retry": {"anyOf": [{"$ref": "#/$defs/Retry"}, {"type": "null"}]},
            },
        },
    })
    gem = ac.responses_to_gemini({
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "describe_tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": schema_out},
            {"type": "function_call", "call_id": "c2", "name": "ping_tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c2", "output": '{"ok":true,"count":2}'},
        ],
    })
    env = ac.wrap_cloud_code(gem, model="gemini-4-carbon", project_id="proj-1")
    user_turns = [c for c in env["request"]["contents"] if c["role"] == "user"]
    assert user_turns[0]["parts"][0]["functionResponse"]["response"] == {"result": schema_out}
    assert user_turns[1]["parts"][0]["functionResponse"]["response"] == {"ok": True, "count": 2}


def test_responses_to_gemini_wraps_top_level_array_ref_without_double_wrapping():
    arr_out = json.dumps([{"$ref": "#/$defs/Node"}, {"name": "ok"}])
    gem = ac.responses_to_gemini({
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "list_schemas", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": arr_out},
        ],
    })
    env = ac.wrap_cloud_code(gem, model="gemini-4-carbon", project_id="proj-1")
    user_turns = [c for c in env["request"]["contents"] if c["role"] == "user"]
    assert user_turns[0]["parts"][0]["functionResponse"]["response"] == {"result": arr_out}


def test_wrap_cloud_code_preserves_valid_part_display_name_ref_and_does_not_mutate_caller():
    raw_gemini = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "render_chart",
                            "response": {"image": {"$ref": "chart.png"}},
                            "parts": [
                                {"inlineData": {"mimeType": "image/png", "data": "AAAA", "displayName": "chart.png"}},
                            ],
                        },
                    },
                    {
                        "functionResponse": {
                            "name": "get_schema",
                            "response": {"item": {"$ref": "#/$defs/Node"}},
                        },
                    },
                ],
            },
        ],
    }
    snapshot = copy.deepcopy(raw_gemini)
    env = ac.wrap_cloud_code(raw_gemini, model="gemini-4-carbon", project_id="proj-1")
    assert raw_gemini == snapshot
    parts = env["request"]["contents"][0]["parts"]
    assert parts[0]["functionResponse"]["response"] == {"image": {"$ref": "chart.png"}}
    assert parts[1]["functionResponse"]["response"] == {"result": '{"item":{"$ref":"#/$defs/Node"}}'}


def test_apply_request_429_without_delay_uses_min_window_for_rpm_and_max_for_quota_or_challenge():
    m = _import_modules()
    _setup(m)
    from src import cooldown
    from src.antigravity.quota import apply_request_429

    om = m["oauth_manager"]
    m["config"].update(lambda cfg: cfg.update({"errorWindows": [1, 3, 5, 10, 15, 0]}))
    ak = _add_ag_account(om, email="rpm@fake.invalid", project="p-rpm", models=["m1", "m2", "m3"])
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))

    now_ms = int(time.time() * 1000)
    apply_request_429(
        ch.state_key, "m1",
        {"error": {"status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"},
        ]}},
        account_key=ak, expected_state_key=ch.state_key,
    )
    rpm_until = cooldown.get_state(ch.key, "m1")["cooldown_until"]
    assert 45_000 <= rpm_until - now_ms <= 90_000

    apply_request_429(
        ch.state_key, "m2",
        {"error": {"status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED"},
        ]}},
        account_key=ak, expected_state_key=ch.state_key,
    )
    quota_until = cooldown.get_state(ch.key, "m2")["cooldown_until"]
    assert 840_000 <= quota_until - now_ms <= 960_000

    apply_request_429(
        ch.state_key, "m3",
        {"error": {"status": "VALIDATION_REQUIRED"}},
        account_key=ak, expected_state_key=ch.state_key, http_status=403,
    )
    val_until = cooldown.get_state(ch.key, "m3")["cooldown_until"]
    assert 840_000 <= val_until - now_ms <= 960_000
