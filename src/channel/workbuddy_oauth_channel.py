"""WorkBuddy CLI OAuth as a native, stream-only OpenAI Chat channel."""
from __future__ import annotations

import copy
import json
import logging

from .. import cache_hints, config, model_names, oauth_manager
from ..oauth.workbuddy import common
from ..oauth_ids import account_key
from ..openai.channel.api_channel import OpenAIApiChannel
from ..openai.transform import anthropic_to_chat, guard
from ..providers import registry as provider_registry
from ..providers.workbuddy_codec import WorkBuddyStream
from ..workbuddy_request_rewrite import rewrite_payload, settings_from_config
from .base import ChannelDisplay, UpstreamRequest, build_dispatch_metadata


CLIENT_IDENTITY = "You are CodeBuddy Code."
_logger = logging.getLogger(__name__)


def add_client_identity(payload: dict) -> None:
    """Add the client identity without rewriting business roles/content/order."""
    messages = payload.get("messages") or []
    identity = {"role": "system", "content": CLIENT_IDENTITY}
    if not messages or messages[0] != identity:
        payload["messages"] = [identity, *messages]


def normalize_tool_choice(payload: dict) -> None:
    """The CLI endpoint accepts a string, never an OpenAI choice object."""
    choice = payload.get("tool_choice")
    if choice is None:
        payload.pop("tool_choice", None)
        return
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind in {"none", "auto", "required"}:
            choice = kind
        elif kind == "function":
            function = choice.get("function") or {}
            choice = function.get("name") if isinstance(function, dict) else None
            choice = choice or payload["tool_choice"].get("name")
        else:
            choice = None
    if not isinstance(choice, str) or not choice.strip():
        raise guard.GuardError(400, "invalid_request_error", "Invalid WorkBuddy tool_choice", param="tool_choice", scope="candidate")
    if choice == "none":
        payload.pop("tool_choice", None)
        payload.pop("tools", None)
        payload.pop("functions", None)
    else:
        payload["tool_choice"] = choice


class WorkBuddyOAuthChannel(OpenAIApiChannel):
    type = "oauth"
    provider = "workbuddy"
    protocol = "openai-chat"
    cc_mimicry = False
    upstream_stream_only = True

    def __init__(self, account: dict):
        models = oauth_manager.account_model_selection(account)["effective_models"]
        name = str(account.get("label") or account.get("nickname") or account["uid"])
        super().__init__({"name": name, "baseUrl": common.api_base_url(account),
            "apiPath": "/v2/chat/completions", "protocol": "openai-chat",
            "models": [{"real": model, "alias": model_names.public_id("workbuddy", model)} for model in models],
            "enabled": account.get("enabled", True), "disabled_reason": account.get("disabled_reason"),
            "responsesWsUpstreamTransport": "sse"})
        self.account = copy.deepcopy(account)
        self.account_key = account_key(account)
        self.key = "oauth:" + self.account_key
        self.email = str(account.get("email") or name)
        self.type, self.provider = "oauth", "workbuddy"
        try:
            self.max_concurrent = max(0, int(account.get("maxConcurrent") or 0))
        except (ValueError, TypeError):
            self.max_concurrent = 0

    def supports_model(self, requested_model: str):
        real = model_names.upstream_id(self.provider, requested_model)
        return real if any(item.get("real") == real for item in self.models) else None

    def _is_deepseek(self, model=None) -> bool:
        # A model name must not activate another vendor's defaults or replay rules.
        return False

    def _apply_compatibility(self, payload, resolved_model, *, requested_model=None):
        # WorkBuddy owns its exact effort catalog; never clamp or inject high.
        pass

    def _build_anthropic_to_chat(self, body: dict, resolved_model: str) -> UpstreamRequest:
        payload = anthropic_to_chat.translate_request(body, target_model=None)
        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(self, payload, protocol=self.protocol)
        cache_hints.apply_anthropic_cache_to_openai_payload(body, payload, model=resolved_model,
            api_key_name=body.get("_parrot_api_key_name"), client_ip=body.get("_parrot_client_ip"))
        return UpstreamRequest(url=self.base_url + self.api_path, headers={},
            body=json.dumps(payload, ensure_ascii=False).encode(), translator_ctx={
                "ingress": "anthropic", "upstream_protocol": self.protocol,
                "response_translator": "anthropic_to_chat", "model_for_response": resolved_model})

    async def build_upstream_request(self, requested_body, resolved_model, *, ingress_protocol="anthropic"):
        current = oauth_manager.get_account(self.account_key)
        if not current or resolved_model not in oauth_manager.account_model_selection(current)["effective_models"]:
            raise guard.GuardError(400, "invalid_request_error", "WorkBuddy model unavailable for this account", param="model", scope="candidate")
        request = await super().build_upstream_request(copy.deepcopy(requested_body), resolved_model, ingress_protocol=ingress_protocol)
        payload = json.loads(request.body)
        if payload.get("n", 1) not in (None, 1):
            raise guard.GuardError(400, "invalid_request_error", "WorkBuddy supports one completion per request", param="n", scope="candidate")
        if payload.get("functions") or payload.get("function_call"):
            raise guard.GuardError(400, "invalid_request_error", "Use tools/tool_choice with WorkBuddy", param="functions", scope="candidate")
        catalog = current.get("account_model_catalog") or {}
        records = catalog.get("models") or []
        record = next((r for r in records if isinstance(r, dict) and r.get("id") == resolved_model), {})
        efforts = record.get("reasoningEfforts") or []
        effort = payload.get("reasoning_effort")
        if effort is not None and efforts and effort not in efforts:
            raise guard.GuardError(400, "invalid_request_error", "Requested reasoning_effort is not supported by this WorkBuddy model", param="reasoning_effort", scope="candidate")
        normalize_tool_choice(payload)
        # All ingress protocols have converged to Chat here. Keep adaptation
        # provider/profile-local and read the current config on every dispatch.
        if common.realm_of(current) == "cn" and common.profile_of(current) == common.PROFILE:
            payload, hits = rewrite_payload(payload, settings_from_config(config.get()))
            for rule_id, count in hits.items():
                _logger.debug("WorkBuddy request rewrite rule=%s hits=%d", rule_id, count)
        add_client_identity(payload)
        payload["stream"] = True
        payload["stream_options"] = {**(payload.get("stream_options") or {}), "include_usage": True}
        token = await oauth_manager.ensure_channel_token(self)
        current = oauth_manager.get_account(self.account_key)
        if not current:
            raise common.WorkBuddyError("chat", kind="stale_generation")
        headers = common.headers(dict(current, access_token=token), "chat")
        headers["Accept"] = "text/event-stream"
        request.url = common.api_base_url(current) + "/v2/chat/completions"
        request.headers = headers
        request.body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        request.translator_ctx = model_names.response_context(
            self.provider, resolved_model, request.translator_ctx, ingress=ingress_protocol,
        )
        request.translator_ctx = {**(request.translator_ctx or {}), "workbuddy_stream": WorkBuddyStream()}
        request.dispatch_metadata = build_dispatch_metadata(payload, self.protocol, headers)
        return request

    async def restore_response(self, chunk, dynamic_map=None):
        return chunk

    def display(self):
        return ChannelDisplay(self.key, self.type, self.display_name, self.enabled, self.disabled_reason, self.list_client_models())
