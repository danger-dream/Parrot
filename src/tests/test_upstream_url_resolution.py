import json
from types import SimpleNamespace

import httpx
import pytest

from src import config, oauth_manager, oauth_model_discovery
from src.channel.api_channel import ApiChannel
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.channel.url_utils import resolve_api_endpoint, resolve_upstream_url, split_base_url
from src.openai.channel.api_channel import OpenAIApiChannel
from src.xai.imagine import _media_url
from src.models_discovery import derive_custom_models_url
from src import models_discovery
from src.management_auth import AuthMethod, ManagementPrincipal
from src.management_control import ManagementContext
from src.management_control.channels import ChannelControl, DiscoveryCommand
from src.openai.images_runtime import _api_url
from src.providers.catalog import PROVIDER_CATALOG


@pytest.mark.parametrize("suffix", ["/v1/messages", "/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("prefix", ["", "/gateway", "/anthropic"])
@pytest.mark.parametrize("version", ["", "/v1", "/v4", "/v1beta1"])
def test_default_endpoint_preserves_prefix_and_existing_version(suffix, prefix, version):
    base = "https://upstream.test" + prefix + version
    expected = base + (suffix[3:] if version else suffix)
    assert resolve_upstream_url(base, None, suffix) == expected
    assert resolve_upstream_url(base + "/", None, suffix) == expected


@pytest.mark.parametrize("path", [
    "/v1/messages", "/v1/chat/completions", "/v1/responses",
    "/api/coding/paas/v4/chat/completions", "/gateway/responses",
])
def test_complete_endpoint_matches_management_normalization(path):
    raw = "https://upstream.test" + path
    base, api_path = split_base_url(raw)
    assert resolve_upstream_url(raw, None, "/v1/responses") == raw
    assert resolve_upstream_url(base, api_path, "/v1/responses") == raw


@pytest.mark.parametrize(("base", "path", "expected"), [
    ("https://upstream.test", "/custom/responses", "https://upstream.test/custom/responses"),
    ("https://upstream.test/gateway", "/custom/responses", "https://upstream.test/gateway/custom/responses"),
    ("https://upstream.test/v1", "/responses", "https://upstream.test/v1/responses"),
    ("https://upstream.test/gateway/v1", "/v1/responses", "https://upstream.test/gateway/v1/v1/responses"),
    ("https://upstream.test/gateway/v4", "/v4/chat/completions", "https://upstream.test/gateway/v4/v4/chat/completions"),
    ("https://upstream.test/v4", "/v1/responses", "https://upstream.test/v4/v1/responses"),
])
def test_explicit_path_keeps_custom_semantics(base, path, expected):
    assert resolve_upstream_url(base, path, "/v1/messages") == expected


def test_version_like_proxy_name_is_not_a_version_directory():
    assert resolve_upstream_url("https://upstream.test/proxy-v1", None, "/v1/messages") == "https://upstream.test/proxy-v1/v1/messages"


@pytest.mark.parametrize(("path", "root"), [
    ("", "/v1"), ("/v1", "/v1"), ("/gateway", "/gateway/v1"),
    ("/gateway/v1", "/gateway/v1"), ("/api/paas/v4", "/api/paas/v4"),
    ("/v1/responses", "/v1"), ("/v1/chat/completions", "/v1"),
    ("/api/coding/paas/v4/chat/completions", "/api/coding/paas/v4"),
    ("/chat/completions", ""), ("/custom/responses", "/custom"),
    ("/v1/models", "/v1"), ("/v1/images/edits", "/v1"),
])
def test_models_and_images_use_the_same_api_root(path, root):
    base = "https://upstream.test" + path
    expected_root = "https://upstream.test" + root
    assert derive_custom_models_url(base) == expected_root + "/models"
    assert _api_url(SimpleNamespace(base_url=base, api_path=None), "generate") == expected_root + "/images/generations"
    assert _api_url(SimpleNamespace(base_url=base, api_path=None), "edit") == expected_root + "/images/edits"


@pytest.mark.parametrize(("base", "path", "root"), [
    ("https://upstream.test", "/api/coding/paas/v4/chat/completions", "/api/coding/paas/v4"),
    ("https://upstream.test/gateway/v1", "/responses", "/gateway/v1"),
    ("https://upstream.test/gateway", "/custom/chat/completions", "/gateway/custom"),
])
def test_sibling_endpoints_preserve_explicit_paths(base, path, root):
    assert derive_custom_models_url(base, path) == "https://upstream.test" + root + "/models"
    channel = SimpleNamespace(base_url=base, api_path=path)
    assert _api_url(channel, "edit") == "https://upstream.test" + root + "/images/edits"


@pytest.mark.parametrize(("path", "root"), [
    ("", "/v1"), ("/v1", "/v1"), ("/gateway/v1", "/gateway/v1"),
    ("/gateway", "/gateway"), ("/v1/custom", "/v1/custom"),
    ("/v1/responses", "/v1"), ("/v1/language-models", "/v1"),
])
def test_provider_custom_root_is_not_reinterpreted(path, root):
    for endpoint in ("/responses", "/models", "/language-models", "/images/generations", "/videos"):
        assert resolve_api_endpoint("https://upstream.test" + path, endpoint, preserve_custom_path=True) == "https://upstream.test" + root + endpoint


def test_every_provider_preset_round_trips_without_path_changes():
    count = 0
    for provider in PROVIDER_CATALOG:
        for preset in provider.presets:
            for protocol, endpoint in preset.protocols.items():
                base, path = split_base_url(endpoint)
                suffix = {"anthropic": "/v1/messages", "openai-chat": "/v1/chat/completions", "openai-responses": "/v1/responses"}[protocol]
                assert resolve_upstream_url(base, path, suffix) == endpoint
                assert resolve_upstream_url(endpoint, None, suffix) == endpoint
                count += 1
    assert count > 0


def _request_body(ingress, stream=False):
    body = {"model": "routing-fixture", "stream": stream}
    if ingress == "responses":
        body.update(input="Hello", max_output_tokens=32)
    else:
        body.update(messages=[{"role": "user", "content": "Hello"}], max_tokens=32)
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["anthropic", "openai-chat", "openai-responses"])
@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("path", ["", "/v1", "/gateway/v1"])
@pytest.mark.parametrize("stream", [False, True])
async def test_real_api_builders_all_ingress_protocols(protocol, ingress, path, stream):
    endpoint = {"anthropic": "/messages", "openai-chat": "/chat/completions", "openai-responses": "/responses"}[protocol]
    cls = ApiChannel if protocol == "anthropic" else OpenAIApiChannel
    channel = cls({"name": "routing-fixture", "baseUrl": "https://upstream.test" + path,
                   "protocol": protocol, "apiKey": "test-key", "cc_mimicry": False,
                   "models": [{"real": "routing-fixture", "alias": "routing-fixture"}]})
    request = await channel.build_upstream_request(_request_body(ingress, stream), "routing-fixture", ingress_protocol=ingress)
    root = path or "/v1"
    assert request.url == "https://upstream.test" + root + endpoint
    payload = json.loads(request.body)
    assert payload["model"] == "routing-fixture"
    assert payload["stream"] is stream


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "root"), [
    ("", "/v1"), ("/v1", "/v1"), ("/gateway/v1", "/gateway/v1"),
    ("/custom", "/custom"), ("/v1/responses", "/v1"),
])
async def test_xai_text_media_and_discovery_share_the_api_root(monkeypatch, path, root):
    async def token(_channel):
        return "test-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    monkeypatch.setattr(config, "get", lambda: {"xaiOAuth": {"responsesPath": ""}})
    base = "https://upstream.test" + path
    account = {"provider": "xai", "email": "routing@example.test", "subject": "routing",
               "base_url": base, "models": ["routing-fixture"], "access_token": "test-token"}
    channel = XAIOAuthChannel(account)
    request = await channel.build_upstream_request(_request_body("responses"), "routing-fixture", ingress_protocol="responses")
    expected = "https://upstream.test" + root
    assert request.url == expected + "/responses"
    for endpoint in ("images/generations", "images/edits", "videos/generations", "videos/job-id"):
        assert _media_url(channel, endpoint) == expected + "/" + endpoint
    calls = []
    def catalog(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": [{"id": "routing-fixture"}]})
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", catalog)
    assert oauth_model_discovery.discover_xai(account).models == ["routing-fixture"]
    assert calls == [expected + "/language-models", expected + "/models"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "expected"), [
    ("/custom/responses", "https://upstream.test/v1/custom/responses"),
    ("/v1/responses", "https://upstream.test/v1/v1/responses"),
])
async def test_xai_explicit_responses_path_is_preserved(monkeypatch, path, expected):
    async def token(_channel):
        return "test-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    monkeypatch.setattr(config, "get", lambda: {"xaiOAuth": {}})
    channel = XAIOAuthChannel({"provider": "xai", "email": "routing@example.test", "subject": "routing",
        "base_url": "https://upstream.test/v1", "apiPath": path, "models": ["routing-fixture"]})
    request = await channel.build_upstream_request(_request_body("responses"), "routing-fixture", ingress_protocol="responses")
    assert request.url == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(("path", "api_path", "protocol", "root"), [
    ("", None, "openai-chat", "/v1"),
    ("/v1", None, "openai-responses", "/v1"),
    ("/gateway/v1/", None, "anthropic", "/gateway/v1"),
    ("/v1/chat/completions", None, "openai-chat", "/v1"),
    ("/gateway/v1/responses", None, "openai-responses", "/gateway/v1"),
    ("/api/coding/paas/v4/chat/completions", None, "openai-chat", "/api/coding/paas/v4"),
    ("", "/custom/chat/completions", "openai-chat", "/custom"),
    ("/relay", "/custom/responses", "openai-responses", "/relay/custom"),
    ("/api/coding/paas/v4", None, "openai-chat", "/api/coding/paas/v4"),
    ("/chat/completions", None, "openai-chat", ""),
])
async def test_management_model_discovery_then_model_call_uses_same_root(
    monkeypatch, existing, path, api_path, protocol, root,
):
    base = "https://upstream.test" + path
    cls = ApiChannel if protocol == "anthropic" else OpenAIApiChannel
    channel = cls({"name": "routing-fixture", "baseUrl": base, "apiPath": api_path,
                   "protocol": protocol, "apiKey": "test-model-key", "cc_mimicry": False,
                   "models": [{"real": "routing-fixture", "alias": "routing-fixture"}]})
    endpoint = {"anthropic": "/messages", "openai-chat": "/chat/completions", "openai-responses": "/responses"}[protocol]
    ingress = {"anthropic": "anthropic", "openai-chat": "chat", "openai-responses": "responses"}[protocol]
    expected_root = "https://upstream.test" + root
    requests = []
    def wire(request):
        requests.append((request.method, str(request.url)))
        assert request.headers.get("authorization") == "Bearer test-model-key" or request.headers.get("x-api-key") == "test-model-key"
        if request.method == "GET":
            assert str(request.url) == expected_root + "/models"
            return httpx.Response(200, json={"data": [{"id": "routing-fixture"}]})
        assert request.method == "POST"
        assert str(request.url) == expected_root + endpoint
        assert json.loads(request.content)["model"] == "routing-fixture"
        return httpx.Response(200, json={"model": "routing-fixture"})
    transport = httpx.MockTransport(wire)
    monkeypatch.setattr(models_discovery.network, "async_client", lambda **kwargs: httpx.AsyncClient(transport=transport))
    control = ChannelControl()
    monkeypatch.setattr(control, "_domain_channel", lambda _channel_id: channel)
    context = ManagementContext(request_id="routing-discovery", actor=ManagementPrincipal.administrator(
        subject_id="routing-discovery", auth_method=AuthMethod.MANAGEMENT_KEY,
    ))
    command = DiscoveryCommand(channel_id=channel.key) if existing else DiscoveryCommand(
        base_url=base, api_path=api_path, api_key="test-model-key",
    )
    before = json.dumps(config.get(), sort_keys=True)
    result = await control.discover_model_ids(context, command)
    assert result.error is None
    assert result.models == ("routing-fixture",)
    request = await channel.build_upstream_request(_request_body(ingress), result.models[0], ingress_protocol=ingress)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(request.url, headers=request.headers, content=request.body)
    assert response.status_code == 200
    assert requests == [("GET", expected_root + "/models"), ("POST", expected_root + endpoint)]
    assert json.dumps(config.get(), sort_keys=True) == before
