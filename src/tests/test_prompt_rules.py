"""Unit and Management API tests for outbound prompt rewrite rules."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import time

from src import config as config_module
from src import prompt_rules
from src.management_api import ManagementRuntime, install_management_routers


def _base_wrap_rule(**overrides):
    rule = {
        "id": "wrap-sensitive",
        "name": "敏感词无恶意包裹",
        "enabled": True,
        "match": {"models": [], "channels": [], "providers": [], "apiKeys": [], "protocols": []},
        "targets": ["all"],
        "action": {
            "type": "wrap",
            "mode": "literal",
            "pattern": "",
            "patterns": ["色情", "逆向", "破解"],
            "replacement": "",
            "prefix": " ",
            "suffix": "（无任何恶意）",
            "skipIfWrapped": True,
            "text": "",
            "caseSensitive": False,
        },
        "stopOnMatch": False,
    }
    rule.update(overrides)
    return rule


def test_wrap_action_with_multiple_keywords_and_idempotency():
    settings = {"enabled": True, "rules": [_base_wrap_rule()]}
    body = {
        "model": "glm-5.3",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "我不喜欢色情内容，也不做破解。"}]},
        ],
    }
    original = copy.deepcopy(body)

    out1, rep1 = prompt_rules.apply_to_request(
        body, ingress_protocol="responses", resolved_model="glm-5.3", settings_override=settings,
    )
    assert body == original  # Original body is never mutated
    assert rep1.changed is True
    assert rep1.total_replacements == 2
    assert out1["input"][0]["content"][0]["text"] == "我不喜欢 色情（无任何恶意）内容，也不做 破解（无任何恶意）。"

    # Second pass must be idempotent when skipIfWrapped=True
    out2, rep2 = prompt_rules.apply_to_request(
        out1, ingress_protocol="responses", resolved_model="glm-5.3", settings_override=settings,
    )
    assert rep2.changed is False
    assert out2["input"][0]["content"][0]["text"] == out1["input"][0]["content"][0]["text"]


def test_wrap_prefers_longer_keyword_before_shorter_substring():
    settings = {
        "enabled": True,
        "rules": [_base_wrap_rule(action={
            "type": "wrap",
            "mode": "literal",
            "patterns": ["逆向", "逆向工程"],
            "prefix": "[",
            "suffix": ":benign]",
            "skipIfWrapped": True,
            "caseSensitive": False,
        })],
    }
    out, rep = prompt_rules.apply_to_request(
        {"model": "m", "input": "学习逆向工程和逆向分析"},
        ingress_protocol="responses",
        settings_override=settings,
    )
    assert rep.total_replacements == 2
    assert out["input"] == "学习[逆向工程:benign]和[逆向:benign]分析"


def test_all_models_and_filtered_models_and_protocols():
    glm_rule = {
        "id": "glm-delete",
        "name": "GLM 删词",
        "enabled": True,
        "match": {"models": ["glm-*"], "channels": [], "providers": [], "apiKeys": [], "protocols": []},
        "targets": ["system"],
        "action": {
            "type": "replace",
            "mode": "literal",
            "pattern": "think step by step",
            "replacement": "",
            "caseSensitive": False,
        },
    }
    settings = {"enabled": True, "rules": [glm_rule]}
    body = {"model": "GLM-5.3", "instructions": "Please THINK STEP BY STEP carefully.", "input": "think step by step"}

    # Matches glm-* (case-insensitive) and only touches system, not user input
    out_glm, rep_glm = prompt_rules.apply_to_request(
        body, ingress_protocol="responses", resolved_model="glm-5.3", settings_override=settings,
    )
    assert rep_glm.changed is True
    assert out_glm["instructions"] == "Please  carefully."
    assert out_glm["input"] == "think step by step"

    # Does not match claude
    out_claude, rep_claude = prompt_rules.apply_to_request(
        {**body, "model": "claude-opus-5-5"},
        ingress_protocol="responses",
        resolved_model="claude-opus-5-5",
        settings_override=settings,
    )
    assert rep_claude.changed is False
    assert out_claude is not out_glm


def test_strict_protection_of_tool_args_media_and_thinking_signatures():
    settings = {
        "enabled": True,
        "rules": [{
            "id": "replace-secret",
            "name": "替换敏感词",
            "enabled": True,
            "match": {},
            "targets": ["all"],
            "action": {"type": "replace", "mode": "literal", "pattern": "SECRET", "replacement": "CLEAN"},
        }],
    }

    # Responses protocol: function_call.arguments, reasoning.encrypted_content, input_image must stay intact
    resp_body = {
        "model": "m",
        "instructions": "sys SECRET",
        "tools": [
            {"type": "function", "name": "t1", "description": "desc SECRET", "parameters": {"type": "object"}},
            {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": "sub", "description": "ns SECRET"}]},
        ],
        "input": [
            {"role": "user", "content": [
                {"type": "input_text", "text": "user SECRET"},
                {"type": "input_image", "image_url": "data:image/png;base64,SECRET"},
            ]},
            {"type": "reasoning", "summary": [], "encrypted_content": "SECRET_CIPHERTEXT"},
            {"type": "function_call", "call_id": "c1", "name": "t1", "arguments": '{"k":"SECRET"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "result SECRET"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "asst SECRET"}]},
        ],
    }
    out, rep = prompt_rules.apply_to_request(resp_body, ingress_protocol="responses", settings_override=settings)
    assert rep.changed is True
    assert out["instructions"] == "sys CLEAN"
    assert out["tools"][0]["description"] == "desc CLEAN"
    assert out["tools"][1]["tools"][0]["description"] == "ns CLEAN"
    assert out["input"][0]["content"][0]["text"] == "user CLEAN"
    assert out["input"][0]["content"][1]["image_url"] == "data:image/png;base64,SECRET"
    assert out["input"][1]["encrypted_content"] == "SECRET_CIPHERTEXT"
    assert out["input"][2]["arguments"] == '{"k":"SECRET"}'
    assert out["input"][3]["output"] == "result CLEAN"
    assert out["input"][4]["content"][0]["text"] == "asst CLEAN"

    # Anthropic protocol: thinking.signature and tool_use.input must stay intact
    anth_body = {
        "model": "claude",
        "system": [{"type": "text", "text": "sys SECRET"}],
        "tools": [{"name": "t", "description": "tool SECRET", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "user SECRET"},
                {"type": "tool_result", "tool_use_id": "u1", "content": "tr SECRET"},
            ]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "SECRET", "signature": "SIG_SECRET"},
                {"type": "tool_use", "id": "u1", "name": "t", "input": {"raw": "SECRET"}},
                {"type": "text", "text": "asst SECRET"},
            ]},
        ],
    }
    out_a, _ = prompt_rules.apply_to_request(anth_body, ingress_protocol="anthropic", settings_override=settings)
    assert out_a["system"][0]["text"] == "sys CLEAN"
    assert out_a["tools"][0]["description"] == "tool CLEAN"
    assert out_a["messages"][0]["content"][0]["text"] == "user CLEAN"
    assert out_a["messages"][0]["content"][1]["content"] == "tr CLEAN"
    assert out_a["messages"][1]["content"][0] == {"type": "thinking", "thinking": "SECRET", "signature": "SIG_SECRET"}
    assert out_a["messages"][1]["content"][1]["input"] == {"raw": "SECRET"}
    assert out_a["messages"][1]["content"][2]["text"] == "asst CLEAN"


def test_prepend_and_append_system_across_protocols_and_idempotency():
    settings = {
        "enabled": True,
        "rules": [
            {"id": "pre", "name": "pre", "enabled": True, "match": {}, "targets": ["system"], "action": {"type": "prepend_system", "text": "[HEAD]"}},
            {"id": "app", "name": "app", "enabled": True, "match": {}, "targets": ["system"], "action": {"type": "append_system", "text": "[TAIL]"}},
        ],
    }
    chat_body = {"model": "ds", "messages": [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}]}
    out_chat, rep_chat = prompt_rules.apply_to_request(chat_body, ingress_protocol="chat", settings_override=settings)
    assert rep_chat.total_replacements == 2
    assert out_chat["messages"][0]["content"] == "[HEAD]\n\nbase\n\n[TAIL]"
    # Re-applying is idempotent
    out_chat2, rep_chat2 = prompt_rules.apply_to_request(out_chat, ingress_protocol="chat", settings_override=settings)
    assert rep_chat2.changed is False
    assert out_chat2["messages"][0]["content"] == out_chat["messages"][0]["content"]

    anth_no_sys = {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}
    out_anth, _ = prompt_rules.apply_to_request(anth_no_sys, ingress_protocol="anthropic", settings_override=settings)
    assert out_anth["system"] == "[HEAD]\n\n[TAIL]"


def test_validation_rejects_unsafe_regex_and_missing_wrap_affixes():
    with pytest.raises(prompt_rules.PromptRuleValidationError):
        prompt_rules.normalize_rule({
            "id": "bad-regex",
            "action": {"type": "replace", "mode": "regex", "pattern": "(a+)+$"},
        })
    with pytest.raises(prompt_rules.PromptRuleValidationError):
        prompt_rules.normalize_rule({
            "id": "empty-wrap",
            "action": {"type": "wrap", "mode": "literal", "patterns": ["x"], "prefix": "", "suffix": ""},
        })


def test_failover_helper_is_per_candidate_and_non_mutating(monkeypatch):
    from src import failover
    monkeypatch.setattr(
        config_module,
        "get",
        lambda: {
            "promptRules": {
                "enabled": True,
                "rules": [{
                    "id": "glm-only",
                    "name": "glm-only",
                    "enabled": True,
                    "match": {"models": ["glm-*"]},
                    "targets": ["system"],
                    "action": {"type": "replace", "mode": "literal", "pattern": "BAD", "replacement": "GOOD"},
                }],
            },
        },
    )
    shared_body = {"model": "glm-5.3", "instructions": "sys BAD", "input": "hi"}
    ch_glm = SimpleNamespace(key="oauth:zhipu:a", display_name="zhipu-a", provider="zhipu")
    ch_claude = SimpleNamespace(key="oauth:claude:b", display_name="claude-b", provider="claude")

    glm_out = failover._apply_candidate_prompt_rules(ch_glm, "glm-5.3", shared_body, ingress_protocol="responses")
    claude_out = failover._apply_candidate_prompt_rules(ch_claude, "claude-opus-5-5", {**shared_body, "model": "claude-opus-5-5"}, ingress_protocol="responses")

    assert glm_out["instructions"] == "sys GOOD"
    assert shared_body["instructions"] == "sys BAD"
    assert claude_out["instructions"] == "sys BAD"


def test_management_api_prompt_rules_crud_reorder_cas_and_dry_run(tmp_path, monkeypatch):
    import server
    key = "pmk_" + "P" * 64
    app = FastAPI()
    monkeypatch.setattr(
        server.config,
        "management_settings",
        lambda: {
            "managementKey": key,
            "stateDbPath": str(tmp_path / "mgmt.db"),
            "allowedOrigins": (),
            "sessionIdleTimeoutSeconds": 3600,
            "sessionAbsoluteTimeoutSeconds": 7200,
            "sessionTouchIntervalSeconds": 60,
            "telegramApprovalTtlSeconds": 180,
            "authRateLimitWindowSeconds": 60,
            "authRateLimitPerSource": 50,
            "authRateLimitGlobal": 200,
            "maxOperations": 100,
            "maxAuditRecords": 100,
        },
    )
    from src.management_api.error_mapping import install_management_error_handlers
    runtime = server._initialize_management_runtime(app)
    install_management_error_handlers(app)
    install_management_routers(app)

    try:
        with TestClient(app) as client:
            sess = client.post("/api/management/v1/auth/sessions", json={"grantType": "managementKey", "managementKey": key})
            assert sess.status_code == 201
            headers = {"Authorization": f"Bearer {sess.json()['data']['credential']}"}

            # Initial empty get
            r0 = client.get("/api/management/v1/prompt-rules", headers=headers)
            assert r0.status_code == 200
            d0 = r0.json()["data"]
            assert d0["enabled"] is True and d0["rules"] == []
            rev0 = d0["revision"]

            # Create rule 1 (wrap)
            r1 = client.post(
                "/api/management/v1/prompt-rules/rules",
                headers={**headers, "If-Match": rev0},
                json=_base_wrap_rule(),
            )
            assert r1.status_code == 201, r1.text
            rev1 = r1.json()["data"]["revision"]
            assert len(r1.json()["data"]["rules"]) == 1

            # Stale revision rejected with 409
            stale = client.patch(
                "/api/management/v1/prompt-rules/settings",
                headers={**headers, "If-Match": rev0},
                json={"enabled": False},
            )
            assert stale.status_code == 409

            # Create rule 2 (replace)
            r2 = client.post(
                "/api/management/v1/prompt-rules/rules",
                headers={**headers, "If-Match": rev1},
                json={
                    "id": "glm-trim",
                    "name": "GLM 删词",
                    "enabled": True,
                    "match": {"models": ["glm-*"]},
                    "targets": ["system"],
                    "action": {"type": "replace", "mode": "literal", "patterns": ["overthink"], "replacement": ""},
                },
            )
            assert r2.status_code == 201
            rev2 = r2.json()["data"]["revision"]

            # Reorder
            ro = client.put(
                "/api/management/v1/prompt-rules/order",
                headers={**headers, "If-Match": rev2},
                json={"ruleIds": ["glm-trim", "wrap-sensitive"]},
            )
            assert ro.status_code == 200
            assert [x["id"] for x in ro.json()["data"]["rules"]] == ["glm-trim", "wrap-sensitive"]
            rev3 = ro.json()["data"]["revision"]

            # Dry-run test
            rt = client.post(
                "/api/management/v1/prompt-rules/test",
                headers=headers,
                json={
                    "model": "glm-5.3",
                    "protocol": "responses",
                    "samples": {"system": "do not overthink", "user_text": "我不喜欢色情内容。"},
                },
            )
            assert rt.status_code == 200, rt.text
            td = rt.json()["data"]
            assert td["changed"] is True
            assert td["totalReplacements"] == 2
            assert td["after"]["system"] == "do not "
            assert td["after"]["user_text"] == "我不喜欢 色情（无任何恶意）内容。"

            # Update & delete
            ru = client.patch(
                "/api/management/v1/prompt-rules/rules/glm-trim",
                headers={**headers, "If-Match": rev3},
                json={
                    "name": "GLM 删词（已更新）",
                    "enabled": False,
                    "match": {"models": ["glm-*"]},
                    "targets": ["system"],
                    "action": {"type": "replace", "mode": "literal", "patterns": ["overthink"], "replacement": ""},
                },
            )
            assert ru.status_code == 200
            rev4 = ru.json()["data"]["revision"]
            assert ru.json()["data"]["rules"][0]["enabled"] is False

            rd = client.delete(
                "/api/management/v1/prompt-rules/rules/glm-trim",
                headers={**headers, "If-Match": rev4},
            )
            assert rd.status_code == 200
            assert [x["id"] for x in rd.json()["data"]["rules"]] == ["wrap-sensitive"]
            rev5 = rd.json()["data"]["revision"]

            # Create rule without explicit id -> auto-generated rule-<hex>
            ra = client.post(
                "/api/management/v1/prompt-rules/rules",
                headers={**headers, "If-Match": rev5},
                json={
                    "name": "自动生成ID规则",
                    "enabled": True,
                    "match": {},
                    "targets": ["all"],
                    "action": {"type": "wrap", "mode": "literal", "patterns": ["逆向"], "prefix": " ", "suffix": "（无任何恶意）"},
                },
            )
            assert ra.status_code == 201, ra.text
            auto_ids = [x["id"] for x in ra.json()["data"]["rules"] if x["name"] == "自动生成ID规则"]
            assert len(auto_ids) == 1 and auto_ids[0].startswith("rule-")
    finally:
        # Also unbinds the Telegram adapters, which would otherwise keep this runtime's controls.
        asyncio.run(server._close_management_runtime(app))


def test_regex_replacement_validation_expansion_guard_and_fail_open(monkeypatch):
    # 1. Invalid regex replacement backreference rejected at validation time
    with pytest.raises(prompt_rules.PromptRuleValidationError, match="replacement template"):
        prompt_rules.normalize_rule({
            "id": "bad-backref",
            "targets": ["all"],
            "action": {"type": "replace", "mode": "regex", "pattern": "foo", "replacement": r"\1"},
        })

    # Valid backreference is allowed
    valid = prompt_rules.normalize_rule({
        "id": "good-backref",
        "targets": ["all"],
        "action": {"type": "replace", "mode": "regex", "pattern": "(foo)", "replacement": r"[\1]"},
    })
    assert valid["action"]["replacement"] == r"[\1]"

    # 2. Runaway expansion cap protects against OOM / payload explosion
    huge_text = "a" * 20000
    huge_rule = {
        "id": "explode",
        "enabled": True,
        "match": {},
        "targets": ["user_text"],
        "action": {"type": "replace", "mode": "literal", "patterns": ["a"], "replacement": "X" * 1000},
    }
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": huge_text}]}
    rewritten, report = prompt_rules.apply_to_request(
        body, ingress_protocol="chat", resolved_model="gpt-5",
        settings_override={"enabled": True, "rules": [huge_rule]},
    )
    assert report.changed is False
    assert rewritten is body

    # 3. O(1) pointer fast-path avoids json.dumps on steady-state config
    live_cfg = {"promptRules": {"enabled": True, "rules": [_base_wrap_rule()]}}
    from src import config as config_module
    monkeypatch.setattr(config_module, "get", lambda: live_cfg)
    prompt_rules._compiled_cache_raw_ref = None
    prompt_rules._compiled_cache_key = ""
    first = prompt_rules._get_live_compiled()
    assert first[0] is True and len(first[1]) == 1

    def _boom_dumps(*_a, **_kw):
        raise AssertionError("json.dumps should not run on pointer cache hit")

    monkeypatch.setattr(prompt_rules.json, "dumps", _boom_dumps)
    second = prompt_rules._get_live_compiled()
    assert second is first

    # 4. Live failover wrapper fails open cleanly if prompt_rules raises unexpectedly
    from src import failover
    from types import SimpleNamespace
    ch = SimpleNamespace(key="oauth:openai:acc1", display_name="acc1")
    monkeypatch.setattr(prompt_rules, "apply_to_request", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
    orig = {"model": "gpt-5", "input": "hello"}
    out = failover._apply_candidate_prompt_rules(ch, "gpt-5", orig, ingress_protocol="responses")
    assert out is orig


def test_anthropic_empty_block_pruning_and_tolerant_live_config(monkeypatch):
    # 1. Deleting an entire Anthropic text block prunes the emptied block cleanly & preserves cache_control
    del_rule = {
        "id": "del-phrase",
        "name": "删词",
        "enabled": True,
        "match": {},
        "targets": ["all"],
        "action": {"type": "replace", "mode": "literal", "patterns": ["DELETE_ME"], "replacement": ""},
    }
    body = {
        "model": "claude-opus-4-6",
        "system": [
            {"type": "text", "text": "Keep system."},
            {"type": "text", "text": "DELETE_ME", "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "DELETE_ME"},
                    {"type": "text", "text": "Real question"},
                ],
            }
        ],
    }
    rewritten, report = prompt_rules.apply_to_request(
        body, ingress_protocol="anthropic", resolved_model="claude-opus-4-6",
        settings_override={"enabled": True, "rules": [del_rule]},
    )
    assert report.changed is True
    assert rewritten["system"] == [{"type": "text", "text": "Keep system.", "cache_control": {"type": "ephemeral"}}]
    assert rewritten["messages"][0]["content"] == [{"type": "text", "text": "Real question"}]
    # Original body must remain untouched
    assert len(body["system"]) == 2
    assert len(body["messages"][0]["content"]) == 2

    # All system blocks emptied -> system key removed cleanly (no invalid empty text block)
    body_all_empty_sys = {
        "model": "claude-opus-4-6",
        "system": [{"type": "text", "text": "DELETE_ME"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    rewritten2, report2 = prompt_rules.apply_to_request(
        body_all_empty_sys, ingress_protocol="anthropic", resolved_model="claude-opus-4-6",
        settings_override={"enabled": True, "rules": [del_rule]},
    )
    assert report2.changed is True
    assert "system" not in rewritten2

    # 2. Tolerant live config compilation: one broken hand-edited rule is skipped, valid rule still works, and result is cached
    live_cfg = {
        "promptRules": {
            "enabled": True,
            "rules": [
                {"id": "bad rule id with spaces!", "action": {"type": "replace", "pattern": "x"}},
                del_rule,
            ],
        }
    }
    from src import config as config_module
    monkeypatch.setattr(config_module, "get", lambda: live_cfg)
    prompt_rules._compiled_cache_raw_ref = None
    prompt_rules._compiled_cache_key = ""
    enabled, compiled = prompt_rules._get_live_compiled()
    assert enabled is True
    assert [r.id for r in compiled] == ["del-phrase"]

    # Second call hits pointer cache without re-hashing even though config contained a skipped bad rule
    monkeypatch.setattr(prompt_rules.json, "dumps", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not re-hash")))
    enabled2, compiled2 = prompt_rules._get_live_compiled()
    assert enabled2 is True and compiled2 is compiled
