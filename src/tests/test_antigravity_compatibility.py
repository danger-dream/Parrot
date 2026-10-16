"""AG compatibility: deterministic protocol inputs, no Google or paid generation."""
from __future__ import annotations

import base64
import copy
import gzip
import json
import time
from pathlib import Path

import pytest

from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_quota_scope import _discover, _catalog_payload, _reset
from src.antigravity.argon import BASE, VARIANTS
from src.openai.transform.guard import GuardError

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6xkAAAAASUVORK5CYII="
DATA = "data:image/png;base64," + PNG
MODEL = BASE + "-medium"


@pytest.fixture(autouse=True)
def clean(m):
    from src import cooldown, model_pricing
    _setup(m)
    m["config"].update(lambda cfg: cfg.update(
        modelBindings={"defaults": {}, "scoped": {}, "legacyMigrationVersion": 1},
        modelMetadata={}, modelMetadataOverrides={"defaults": {}, "scoped": {}}, modelCenter={},
    ))
    cooldown.clear("oauth:antigravity:state@gmail.com:proj-state", notify_recovered=False)
    yield
    cooldown.clear("oauth:antigravity:state@gmail.com:proj-state", notify_recovered=False)
    model_pricing.reset_for_tests()


async def _wire(m, monkeypatch, body, ingress="responses", *, models=None):
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=models or [body["model"]])
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    resolved = ch.resolve_request_model(body["model"], body, ingress)
    req = await ch.build_upstream_request(body, resolved, ingress_protocol=ingress)
    return json.loads(req.body), req, ch


def _tool_body(ingress, url=DATA):
    if ingress == "responses":
        return {"model": MODEL, "input": [
            {"type": "function_call", "call_id": "call_a", "name": "capture", "arguments": json.dumps({"image": {"type": "input_image", "image_url": "https://not-content.example/image"}}), "encrypted_content": "signed-tool-a"},
            {"type": "function_call", "call_id": "call_b", "name": "measure", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_a", "output": [
                {"type": "input_text", "text": "before"}, {"type": "input_image", "image_url": url},
                {"type": "input_text", "text": "after"}]},
            {"type": "function_call_output", "call_id": "call_b", "output": '{"width":1}'},
        ]}
    if ingress == "chat":
        return {"model": MODEL, "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "capture", "arguments": "{}"}},
                {"id": "call_b", "type": "function", "function": {"name": "measure", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_a", "content": [
                {"type": "text", "text": "before"}, {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": "after"}]},
            {"role": "tool", "tool_call_id": "call_b", "content": '{"width":1}'},
        ]}
    source = ({"type": "url", "url": url} if url.startswith("https:") else
              {"type": "base64", "media_type": "image/png", "data": PNG})
    return {"model": MODEL, "max_tokens": 1000, "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_a", "name": "capture", "input": {}, "signature": "signed-tool-a"},
            {"type": "tool_use", "id": "call_b", "name": "measure", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_a", "is_error": True, "content": [
                {"type": "text", "text": "before"}, {"type": "image", "source": source},
                {"type": "text", "text": "after"}]},
            {"type": "tool_result", "tool_use_id": "call_b", "content": '{"width":1}'},
        ]},
    ]}


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.asyncio
async def test_tool_images_are_parts_with_call_identity_not_base64_json(m, monkeypatch, ingress, remote):
    from src.providers import remote_image
    calls = []
    async def download(url):
        calls.append(url)
        return base64.b64decode(PNG), "image/png"
    monkeypatch.setattr(remote_image, "download_https_image", download)
    url = "https://images.example/pixel.png" if remote else DATA
    body = _tool_body(ingress, url)
    before = copy.deepcopy(body)
    wire, req, _ = await _wire(m, monkeypatch, body, ingress)
    assert body == before
    assert calls == ([url] if remote else [])
    parts = [part for content in wire["request"]["contents"] for part in content["parts"]]
    responses = [part["functionResponse"] for part in parts if "functionResponse" in part]
    assert [(r["name"], r["id"]) for r in responses] == [("capture", "call_a"), ("measure", "call_b")]
    result = responses[0]["response"]["result"]
    assert "before" in result and "after" in result and PNG not in result
    if ingress == "anthropic": assert "Tool execution failed" in result
    assert responses[1]["response"] == {"width": 1}
    first = next(i for i, part in enumerate(parts) if part.get("functionResponse", {}).get("id") == "call_a")
    assert parts[first + 1] == {"inlineData": {"mimeType": "image/png", "data": PNG}}
    if ingress == "responses":
        call = next(p for p in parts if p.get("functionCall", {}).get("id") == "call_a")
        assert call["thoughtSignature"] == "signed-tool-a"
        assert call["functionCall"]["args"]["image"]["image_url"] == "https://not-content.example/image"
    assert req.translator_ctx["model_for_response"] == MODEL


@pytest.mark.parametrize("part", [
    {"type": "input_file", "file_id": "opaque-file"},
    {"type": "input_image", "image_url": "data:text/plain;base64,YQ=="},
    {"type": "input_image", "image_url": "data:image/png;base64,not-valid!"},
])
@pytest.mark.asyncio
async def test_unsupported_tool_attachments_fail_explicitly(m, monkeypatch, part):
    body = _tool_body("responses")
    body["input"][2]["output"] = [part]
    with pytest.raises(GuardError):
        await _wire(m, monkeypatch, body)


@pytest.mark.asyncio
async def test_tool_remote_image_uses_existing_safety_and_size_limits(m, monkeypatch):
    from src.providers import remote_image
    async def refused(url):
        raise remote_image.RemoteImageError("input_image URL resolves to a non-public address")
    monkeypatch.setattr(remote_image, "download_https_image", refused)
    with pytest.raises(GuardError, match="non-public") as error:
        await _wire(m, monkeypatch, _tool_body("responses", "https://private.example/image"))
    assert error.value.status == 400
    monkeypatch.setattr(remote_image, "MAX_IMAGE_BYTES", 1)
    with pytest.raises(GuardError, match="size limit"):
        await _wire(m, monkeypatch, _tool_body("responses"))


@pytest.mark.asyncio
async def test_tool_json_image_fields_remain_ordinary_result_data(m, monkeypatch):
    body = _tool_body("responses")
    ordinary = {"image": {"type": "input_image", "image_url": "https://private.example/image"}}
    body["input"][2]["output"] = ordinary
    wire, _, _ = await _wire(m, monkeypatch, body)
    result = next(p["functionResponse"] for c in wire["request"]["contents"] for p in c["parts"] if "functionResponse" in p)
    assert result["response"] == ordinary


@pytest.mark.parametrize("active", ["daily", "prod"])
@pytest.mark.parametrize("sparse_active", [False, True])
@pytest.mark.asyncio
async def test_active_catalog_only_selects_active_models_and_keeps_side_metadata(m, monkeypatch, active, sparse_active):
    ag, om = m["ag"], m["oauth_manager"]
    base = ag.daily_api_base_url() if active == "daily" else ag.api_base_url()
    ak = _add_ag_account(om, request_base_url=base, disabledModels=["shared"])
    payloads = {
        "prod": {"models": {"shared": {
            "displayName": "Prod", "maxTokens": 100000, "maxOutputTokens": 8192,
            "supportsThinking": False, "supportsImages": False,
            "inputModalities": ["text"], "outputModalities": ["text"],
            "quotaInfo": {"remainingFraction": .4, "resetTime": _reset()},
        }, "prod-only": {}}},
        "daily": {"models": {"shared": {
            "displayName": "Daily", "maxTokens": 200000, "maxOutputTokens": 32768,
            "supportsThinking": True, "supportsImages": True,
            "inputModalities": ["text", "image"], "outputModalities": ["text"],
            "quotaInfo": {"remainingFraction": .8, "resetTime": _reset()},
        }, "daily-only": {}}},
    }
    expected = payloads[active]["models"]["shared"]
    if sparse_active:
        # Missing active fields are unknown, not permission to borrow side data.
        expected.pop("maxOutputTokens")
        expected.pop("supportsImages")
    result = await _discover(m, monkeypatch, ak, payloads)
    assert result["action"] == "updated"
    saved = om.get_account(ak)
    assert saved["models"] == ["shared", active + "-only"]
    assert saved["disabledModels"] == ["shared"]
    records = {r["id"]: r for r in saved["account_model_catalog"]["models"]}
    other = "prod-only" if active == "daily" else "daily-only"
    assert records[other]["antigravityAvailability"] == "discovery_only"
    ch = m["AntigravityOAuthChannel"](saved)
    assert ch.base_url == base and ch.supports_model(other) is None
    assert ch.supports_model(active + "-only") == active + "-only"
    disk = json.loads(Path(m["config"].CONFIG_PATH).read_text())["oauthAccounts"][0]
    assert disk["account_model_catalog"] == saved["account_model_catalog"]
    assert disk["disabledModels"] == ["shared"]
    for snapshot in (saved, disk):
        catalog = snapshot["account_model_catalog"]
        assert [row["id"] for row in catalog["models"]] == ["shared", "prod-only", "daily-only"]
        shared = catalog["models"][0]
        assert shared["name"] == expected["displayName"]
        assert shared["contextWindow"] == expected["maxTokens"]
        assert shared["supportsThinking"] is expected["supportsThinking"]
        assert shared["reasoning"] is expected["supportsThinking"]
        assert shared["inputModalities"] == expected["inputModalities"]
        assert shared["outputModalities"] == expected["outputModalities"]
        for field in ("maxOutputTokens", "supportsImages"):
            if sparse_active:
                assert field not in shared
            else:
                assert shared[field] == expected[field]
        assert shared["antigravityAvailability"] == "generation"
        sources = shared["antigravitySources"]
        assert [source["endpoint"] for source in sources] == ["prod", "daily"]
        assert [source["baseUrl"] for source in sources] == [ag.api_base_url(), ag.daily_api_base_url()]
        for source in sources:
            assert source["quotaInfo"] == payloads[source["endpoint"]]["models"]["shared"]["quotaInfo"]
            assert type(source["observedAt"]) is int and source["observedAt"] > 0


@pytest.mark.parametrize("bad", [RuntimeError("offline"), {"models": {}}, {"models": {MODEL: None}}, {}])
@pytest.mark.parametrize("active", ["prod", "daily"])
@pytest.mark.asyncio
async def test_active_failure_does_not_replace_lkg_or_release_quota(m, monkeypatch, bad, active):
    from src import cooldown
    om = m["oauth_manager"]
    base = m["ag"].api_base_url() if active == "prod" else m["ag"].daily_api_base_url()
    ak = _add_ag_account(om, request_base_url=base)
    side = "daily" if active == "prod" else "prod"
    payloads = {side: _catalog_payload(.8), active: _catalog_payload(0, _reset())}
    assert (await _discover(m, monkeypatch, ak, payloads))["action"] == "updated"
    before = om.get_account(ak)
    payloads[active] = bad
    payloads[side]["models"]["side-new"] = {}
    assert (await _discover(m, monkeypatch, ak, payloads))["action"] == "error"
    after = om.get_account(ak)
    assert after["models"] == before["models"]
    assert after["account_model_catalog"] == before["account_model_catalog"]
    assert cooldown.is_blocked("oauth:" + ak, MODEL)


@pytest.mark.parametrize("field", ["request_base_url", "requestBaseUrl", "base_url", "baseUrl"])
@pytest.mark.asyncio
async def test_explicit_custom_generation_address_is_shared_by_catalog_and_wire(m, monkeypatch, field):
    ag, om = m["ag"], m["oauth_manager"]
    base = "https://custom.example/ag"
    ak = _add_ag_account(om, **{field: base})
    calls = []
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    monkeypatch.setattr(om, "ensure_channel_token", token)
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    from src.tests.test_antigravity_quota_scope import Response
    def request(method, url, **kwargs):
        calls.append(url)
        return Response(_catalog_payload(0, _reset()))
    monkeypatch.setattr(ag, "request_sync", request)
    assert (await om.refresh_account_models(ak))["action"] == "updated"
    from src import cooldown
    assert cooldown.is_blocked("oauth:" + ak, MODEL)
    assert calls == [base + "/v1internal:fetchAvailableModels"]
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    req = await ch.build_upstream_request({"model": MODEL, "input": "hello"}, MODEL)
    assert req.url == base + "/v1internal:generateContent"


@pytest.mark.asyncio
async def test_route_switch_invalidates_old_sources_but_not_manual_source_less_models(m, monkeypatch):
    om, ag = m["oauth_manager"], m["ag"]
    ak = _add_ag_account(om)
    await _discover(m, monkeypatch, ak, {"prod": _catalog_payload(.8), "daily": _catalog_payload(.8)})
    m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url=ag.api_base_url()))
    saved = om.get_account(ak)
    ch = m["AntigravityOAuthChannel"](saved)
    assert ch.supports_model(MODEL) is None
    assert om._model_catalog_complete(saved, saved["models"]) is False
    saved["models"].append("manual-model")
    manual = m["AntigravityOAuthChannel"](saved)
    assert manual.supports_model("manual-model") == "manual-model"
    await _discover(m, monkeypatch, ak, {"prod": _catalog_payload(.8), "daily": _catalog_payload(.8)})
    assert m["AntigravityOAuthChannel"](om.get_account(ak)).supports_model(MODEL) == MODEL


@pytest.mark.asyncio
async def test_route_change_during_discovery_cannot_commit_old_endpoint(m, monkeypatch):
    from src import oauth_model_discovery
    from src.tests.test_antigravity_quota_scope import Response
    ag, om = m["ag"], m["oauth_manager"]
    ak = _add_ag_account(om)
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    def request(*args, **kwargs):
        m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url=ag.api_base_url()))
        return Response({"models": {MODEL: {}}})
    monkeypatch.setattr(ag, "request_sync", request)
    assert (await om.refresh_account_models(ak))["action"] == "stale"
    assert not om.get_account(ak).get("last_model_sync")


def _body(ingress, model, effort=None, summary=False):
    if ingress == "responses":
        body = {"model": model, "input": "Reply with OK.", "temperature": .4, "top_p": .7}
        if effort is not None or summary:
            body["reasoning"] = {**({"effort": effort} if effort is not None else {}), **({"summary": "auto"} if summary else {})}
    elif ingress == "chat":
        body = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}], "temperature": .4, "top_p": .7}
        if effort is not None: body["reasoning_effort"] = effort
        if summary: body["reasoning_summary"] = "auto"
    else:
        body = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 2048, "temperature": .4, "top_p": .7}
        if effort is not None: body["output_config"] = {"effort": effort}
        if summary: body["thinking"] = {"type": "adaptive"}
    return body


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("level", ["low", "medium", "high"])
@pytest.mark.parametrize("bare", [False, True])
@pytest.mark.asyncio
async def test_argon_supported_intent_preserves_exact_wire_tier_and_summary(m, monkeypatch, ingress, level, bare):
    model = BASE + "-" + level
    body = _body(ingress, BASE if bare else model, level if bare else None, summary=True)
    wire, req, ch = await _wire(m, monkeypatch, body, ingress, models=list(VARIANTS))
    assert wire["model"] == model
    gen = wire["request"]["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingLevel": level, "includeThoughts": True}
    assert gen["temperature"] == .4 and gen["topP"] == .7
    assert req.dispatch_metadata.outbound_model_id == model
    assert req.translator_ctx["model_for_response"] == model


@pytest.mark.parametrize("effort", ["minimal", "none", "xhigh", "max", "mystery", {}, True])
@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_argon_unknown_effort_is_not_promoted(m, monkeypatch, ingress, effort):
    with pytest.raises(GuardError, match="only explicit low, medium or high"):
        await _wire(m, monkeypatch, _body(ingress, MODEL, effort), ingress)


@pytest.mark.asyncio
async def test_argon_conflicts_budget_and_ambiguous_bare_fail_without_default(m, monkeypatch):
    with pytest.raises(GuardError, match="conflicts"):
        await _wire(m, monkeypatch, _body("responses", MODEL, "high"))
    with pytest.raises(GuardError, match="requires explicit"):
        await _wire(m, monkeypatch, _body("responses", BASE), models=[MODEL])
    with pytest.raises(GuardError, match="numeric budget"):
        body = _body("anthropic", BASE + "-high")
        body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        await _wire(m, monkeypatch, body, "anthropic")
    wire, _, _ = await _wire(m, monkeypatch, _body("responses", BASE), models=[BASE])
    assert wire["model"] == BASE
    assert wire["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": False}


@pytest.mark.asyncio
async def test_argon_no_summary_request_and_other_gemini_are_unchanged(m, monkeypatch):
    wire, _, _ = await _wire(m, monkeypatch, _body("responses", MODEL))
    assert wire["request"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium", "includeThoughts": False}
    wire, _, _ = await _wire(m, monkeypatch, _body("responses", "gemini-3-pro", "minimal"))
    assert wire["request"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}


def test_scheduler_resolves_before_cooldown_and_respects_selected_disabled_and_endpoint(m, monkeypatch):
    from src import scheduler, cooldown
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=list(VARIANTS), disabledModels=[BASE + "-high"])
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: [ch])
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *a: False)
    monkeypatch.setattr(scheduler.auth, "channel_allowed", lambda *a: True)
    def candidates(level):
        return scheduler._filter_candidates(BASE, "responses", _body("responses", BASE, level))
    assert candidates("medium")[0] == [(ch, MODEL)]
    assert candidates("high")[0] == [] and candidates("high")[3]
    cooldown.record_error(ch.key, MODEL, "model quota", cooldown_until=int(time.time() * 1000) + 60_000)
    assert candidates("medium")[0] == []
    assert candidates("low")[0] == [(ch, BASE + "-low")]
    m["config"].update(lambda cfg: cfg["modelCenter"].update(disabledModels=[BASE + "-low"]))
    assert candidates("low")[0] == []
    m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(models=[BASE + "-high"]))
    assert candidates("medium")[0] == []  # stale channel cannot add back a deselected model


@pytest.fixture
def official_catalog(monkeypatch, tmp_path):
    from src import model_pricing
    # Exact two-source Argon excerpt from the local models.dev cache, read-only
    # on 2026-10-16. It is a TEST input, not a bundled/production price override.
    excerpt = json.loads((Path(__file__).parent / "fixtures/argon_official_catalog.json").read_text())
    payload = model_pricing._load_json(model_pricing._BUNDLED_PATH)
    payload["api"]["google"]["models"].update(excerpt["api"]["google"]["models"])
    payload["models"].update(excerpt["models"])
    # A reseller with the same bare name must not displace Google's exact identity.
    payload["api"]["reseller-fixture"] = {"models": {BASE: copy.deepcopy(excerpt["api"]["google"]["models"][BASE])}}
    payload["models"]["reseller-fixture/" + BASE] = {"id": "reseller-fixture/" + BASE}
    monkeypatch.setattr(model_pricing.config, "DATA_DIR", str(tmp_path))
    with gzip.open(tmp_path / "models_dev_catalog.json.gz", "wt") as stream:
        json.dump(payload, stream)
    model_pricing.reset_for_tests()
    model_pricing.initialize()
    return payload


def _inventory(models, scope="oauth:antigravity:state@gmail.com:proj-state"):
    from src import model_metadata
    return [model_metadata.ModelInventoryItem(scope, "oauth", "Fixture", model, model) for model in models]


def _settled_attempt(model, *, visible=None, scope="oauth:antigravity:state@gmail.com:proj-state"):
    from src import log_db, model_pricing
    import uuid
    log_db.init()
    handle = log_db.insert_pending(str(uuid.uuid4()), "127.0.0.1", "fixture", visible or model, False,
                                   1, 0, {}, {}, ingress_protocol="responses")
    attempt = log_db.record_retry_attempt(handle, 1, scope, "oauth", model, time.time(),
                                         upstream_protocol="openai-responses", client_visible_model=visible or model)
    log_db.mark_retry_attempt_dispatch(attempt, {"model": model})
    log_db.finish_success(handle, scope, "oauth", model, input_tokens=1000, output_tokens=100,
                          response_body=json.dumps({"model": model, "usage": {"input_tokens": 1000, "output_tokens": 100}}),
                          upstream_protocol="openai-responses", usage_observed=True)
    conn = log_db._get_conn_for_ref(handle.db)
    row = conn.execute("SELECT * FROM retry_chain WHERE id=?", (attempt.row_id,)).fetchone()
    ledger = conn.execute("SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (attempt.row_id,)).fetchone()
    return model_pricing.pricing_binding_from_json(row["binding_json"]), dict(ledger)


@pytest.mark.parametrize("model", list(VARIANTS))
def test_all_argon_tiers_auto_bind_persist_and_settle_actual_attempt(m, model, official_catalog):
    from src import model_metadata, model_pricing
    model_pricing.reset_for_tests()
    assert _settled_attempt(model)[0].tariff is None  # no consumer-side fallback
    result = model_metadata.auto_sync_metadata(_inventory([model]))
    assert result["created"] == [model]
    saved = json.loads(Path(m["config"].CONFIG_PATH).read_text())
    assert saved["modelBindings"]["defaults"][model]["target"] == "google/" + BASE
    binding, ledger = _settled_attempt(model)
    assert binding.outbound_model_id == binding.client_visible_model == model
    assert binding.model_id == BASE and binding.provider_id == "google"
    assert binding.pricing_key == "google/" + BASE
    assert binding.tariff == model_pricing.catalog_tariff("google/" + BASE)
    expected = model_pricing.estimate_cost_from_binding(binding, input_tokens=1000, output_tokens=100)[0]
    assert ledger["cost_source"] == "estimated" and ledger["cost_ticks"] == expected.total_ticks


def test_dynamic_official_catalog_update_changes_new_auto_bound_attempts(m, monkeypatch, tmp_path, official_catalog):
    from src import model_metadata, model_pricing
    model_pricing.reset_for_tests()
    model_metadata.auto_sync_metadata(_inventory(VARIANTS))
    old, old_ledger = _settled_attempt(MODEL)
    payload = copy.deepcopy(official_catalog)
    payload["api"]["google"]["models"][BASE]["cost"]["input"] *= 2
    payload["api"]["google"]["models"][BASE]["cost"]["input_audio"] *= 2
    # The normal local publication/reconcile path, with only its file root isolated.
    monkeypatch.setattr(model_pricing.config, "DATA_DIR", str(tmp_path))
    with gzip.open(tmp_path / "models_dev_catalog.json.gz", "wt") as stream:
        json.dump(payload, stream)
    assert model_pricing.reload_local_catalog()
    assert model_metadata.auto_sync_metadata(_inventory(VARIANTS))["updated"] == sorted(VARIANTS)
    new, ledger = _settled_attempt(MODEL)
    assert new.source_revision != old.source_revision
    assert new.tariff.input_per_token == old.tariff.input_per_token * 2
    assert ledger["cost_ticks"] > old_ledger["cost_ticks"]
    assert model_pricing.pricing_binding_from_json(old.binding_json).tariff == old.tariff
    candidate = model_pricing._candidate_from_payloads(payload["api"], payload["models"])
    for model in VARIANTS:
        assert model_pricing.candidate_canonical_official_model(candidate, model) == "google/" + BASE


def test_missing_official_catalog_never_borrows_reseller_or_strips_other_suffix(m, monkeypatch, tmp_path, official_catalog):
    from src import model_metadata, model_pricing
    payload = copy.deepcopy(official_catalog)
    original = payload["api"]["google"]["models"].pop(BASE)
    payload["api"]["reseller-fixture"] = {"models": {BASE: original}}
    monkeypatch.setattr(model_pricing.config, "DATA_DIR", str(tmp_path))
    with gzip.open(tmp_path / "models_dev_catalog.json.gz", "wt") as stream:
        json.dump(payload, stream)
    assert model_pricing.reload_local_catalog()
    models = [*VARIANTS, BASE + "-xhigh", "unrelated-model-high"]
    result = model_metadata.auto_sync_metadata(_inventory(models))
    assert result["unmatched"] == sorted(models)
    for model in models:
        binding, ledger = _settled_attempt(model)
        assert binding.pricing_key is None and binding.tariff is None
        assert ledger["cost_source"] == "unpriced" and ledger["cost_ticks"] is None


def test_manual_variant_binding_alias_and_scoped_price_override_win(m, official_catalog):
    from src import model_metadata, model_pricing
    scope = "oauth:antigravity:state@gmail.com:proj-state"
    model_pricing.reset_for_tests()
    model_metadata.set_binding(MODEL, "google/" + BASE, source="manual")
    model_metadata.patch_override_fields(MODEL, scope_key=None, outbound_model=None, set_fields={"cost.input": 7.0})
    model_metadata.set_binding(MODEL, "google/" + BASE, source="manual", scope_key=scope, outbound_model=MODEL)
    model_metadata.patch_override_fields(MODEL, scope_key=scope, outbound_model=MODEL, set_fields={"cost.input": 9.0})
    model_metadata.set_binding("operator-alias", "google/" + BASE, source="manual", scope_key=scope, outbound_model=MODEL)
    model_metadata.patch_override_fields("operator-alias", scope_key=scope, outbound_model=MODEL, set_fields={"cost.input": 11.0})
    result = model_metadata.auto_sync_metadata(_inventory([MODEL, "operator-alias"]), include_results=True)
    assert all(r["status"] == "protected" for r in result["results"] if r["modelId"] == MODEL)
    assert m["config"].get()["modelBindings"]["defaults"][MODEL]["source"] == "manual"
    assert _settled_attempt(MODEL)[0].tariff.input_per_token == 9 / 1_000_000
    assert _settled_attempt(MODEL, scope="oauth:antigravity:another:p")[0].tariff.input_per_token == 7 / 1_000_000
    assert _settled_attempt(MODEL, visible="operator-alias")[0].tariff.input_per_token == 11 / 1_000_000


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_bare_tier_schedule_dispatch_response_and_attempt_accounting_agree(m, monkeypatch, ingress, official_catalog, stream):
    import httpx
    from src import scheduler, failover, upstream, log_db, model_metadata, cooldown
    from src.transports import http_runtime
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=list(VARIANTS))
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    ch.tls_fingerprint = None
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: [ch])
    monkeypatch.setattr(scheduler.auth, "channel_allowed", lambda *a: True)
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **k: ([("direct", None)], None))
    model_metadata.auto_sync_metadata(_inventory(VARIANTS))
    body = _body(ingress, BASE, "medium", summary=True)
    body["_client_visible_model"] = BASE
    body["stream"] = stream
    scheduled = scheduler.schedule(body, "fixture", "127.0.0.1", ingress_protocol=ingress)
    assert scheduled.candidates == [(ch, MODEL)]
    seen = []
    def transport(request):
        wire = json.loads(request.content)
        seen.append(wire)
        assert wire["model"] == MODEL
        assert wire["request"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium", "includeThoughts": True}
        payload = {"response": {"candidates": [{"content": {"role": "model", "parts": [
            {"text": "brief summary", "thought": True, "thoughtSignature": "thought-signature"}, {"text": "OK"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "totalTokenCount": 1100}}}
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: " + json.dumps(payload) + "\n\n")
        return httpx.Response(200, json=payload)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(upstream, "_client_pool", upstream._client_pool)
    upstream.set_client(client)
    log_db.init()
    handle = log_db.insert_pending(f"argon-end-to-end-{ingress}-{stream}", "127.0.0.1", "fixture", BASE, stream,
                                   1, 0, {}, body, ingress_protocol=ingress)
    try:
        response = await failover.run_failover(scheduled, body, handle, "fixture", "127.0.0.1", stream,
                                               time.time(), ingress_protocol=ingress)
        assert response.status_code == 200, getattr(response, "body", b"").decode()
        if stream:
            data = b"".join([chunk async for chunk in response.body_iterator]).decode()
            events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]
            visible = [event.get("model") or event.get("response", {}).get("model") or event.get("message", {}).get("model") for event in events]
            assert {model for model in visible if model} == {MODEL}
        else:
            data = response.body.decode()
            assert json.loads(data)["model"] == MODEL
        assert "brief summary" in data
        assert len(seen) == 1
        conn = log_db._get_conn_for_ref(handle.db)
        attempt = conn.execute("SELECT * FROM retry_chain WHERE request_id=?", (handle.request_id,)).fetchone()
        assert attempt["model"] == attempt["client_visible_model"] == MODEL
        assert attempt["binding_pricing_key"] == "google/" + BASE
        ledger = conn.execute("SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (attempt["id"],)).fetchone()
        assert ledger["cost_source"] == "estimated" and ledger["cost_ticks"] > 0
        assert not cooldown.get_state(ch.key, BASE)
        assert body["model"] == BASE  # no cross-candidate mutation
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolved_argon_429_owns_exact_tier_health_not_bare_or_other_tiers(m, monkeypatch):
    import httpx
    from src import scheduler, failover, upstream, log_db, cooldown
    from src.transports import http_runtime
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=list(VARIANTS))
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    ch.tls_fingerprint = None
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: [ch])
    monkeypatch.setattr(scheduler.auth, "channel_allowed", lambda *a: True)
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **k: ([("direct", None)], None))
    seen = []
    def transport(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(429, json={"error": {"code": 429, "message": "synthetic resource exhaustion", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}]}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(upstream, "_client_pool", upstream._client_pool)
    upstream.set_client(client)
    log_db.init()
    body = _body("responses", BASE, "low")
    handle = log_db.insert_pending("argon-health-key", "127.0.0.1", "fixture", BASE, False, 1, 0, {}, body, ingress_protocol="responses")
    try:
        route = scheduler.schedule(body, "fixture", "127.0.0.1", ingress_protocol="responses")
        response = await failover.run_failover(route, body, handle, "fixture", "127.0.0.1", False, time.time(), ingress_protocol="responses")
        assert response.status_code >= 400
        assert seen == [BASE + "-low"]
        assert cooldown.is_blocked(ch.key, BASE + "-low")
        assert not cooldown.is_blocked(ch.key, BASE) and not cooldown.is_blocked(ch.key, MODEL)
        assert om.get_account(ak)["enabled"]
        assert scheduler.schedule(_body("responses", BASE, "medium"), "fixture", "127.0.0.1", ingress_protocol="responses").candidates == [(ch, MODEL)]
        conn = log_db._get_conn_for_ref(handle.db)
        attempt = conn.execute("SELECT model,client_visible_model FROM retry_chain WHERE request_id=?", (handle.request_id,)).fetchone()
        assert tuple(attempt) == (BASE + "-low", BASE + "-low")
    finally:
        await client.aclose()
