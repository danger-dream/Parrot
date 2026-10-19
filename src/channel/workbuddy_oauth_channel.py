"""WorkBuddy CLI OAuth as a native, stream-only OpenAI Chat channel."""
from __future__ import annotations

import copy
import json
import gzip
import logging

from .. import cache_hints, config, model_names, oauth_manager
from ..oauth.workbuddy import common
from ..oauth_ids import account_key
from ..openai.channel.api_channel import OpenAIApiChannel
from ..openai.transform import anthropic_to_chat, guard
from ..providers import registry as provider_registry
from ..providers.workbuddy_codec import WorkBuddyStream
from ..providers import workbuddy_replay, workbuddy_context
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


def merge_tool_call_messages(payload: dict) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    merged = []
    previous_tool_only = False
    for message in messages:
        tool_only = (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("content") in (None, "", [])
            and isinstance(message.get("tool_calls"), list)
            and bool(message["tool_calls"])
            and not (message.keys() - {"role", "content", "tool_calls"})
        )
        # Signed/thinking messages keep their original turn boundaries.
        if tool_only and previous_tool_only:
            merged[-1] = {**merged[-1], "tool_calls": [*merged[-1]["tool_calls"], *message["tool_calls"]]}
        else:
            merged.append(message)
        previous_tool_only = tool_only
    payload["messages"] = merged


def normalize_tool_choice(payload: dict) -> None:
    """Official Chat shape: enum strings or a nested function choice."""
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
            if not isinstance(function, dict):
                raise guard.GuardError(400, "invalid_request_error", "Invalid WorkBuddy tool_choice.function", param="tool_choice", scope="candidate")
            name = function.get("name")
            name = name or choice.get("name")
            if isinstance(name, str) and name.strip():
                payload["tool_choice"] = {"type": "function", "function": {**function, "name": name}}
                return
            choice = None
        else:
            choice = None
    if not isinstance(choice, str) or not choice.strip():
        raise guard.GuardError(400, "invalid_request_error", "Invalid WorkBuddy tool_choice", param="tool_choice", scope="candidate")
    payload["tool_choice"] = choice if choice in {"none", "auto", "required"} else {"type": "function", "function": {"name": choice}}


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
        payload["messages"] = anthropic_to_chat._convert_system(body.get("system")) + workbuddy_replay.anthropic_messages(body.get("messages") or [])
        payload = provider_registry.filter_request_payload(self, payload, protocol=self.protocol)
        cache_hints.apply_anthropic_cache_to_openai_payload(body, payload, model=resolved_model,
            api_key_name=body.get("_parrot_api_key_name"), client_ip=body.get("_parrot_client_ip"))
        return UpstreamRequest(url=self.base_url + self.api_path, headers={},
            body=json.dumps(payload, ensure_ascii=False).encode(), translator_ctx={
                "ingress": "anthropic", "upstream_protocol": self.protocol,
                "response_translator": "anthropic_to_chat", "model_for_response": resolved_model, "request_body": body})

    def _build_responses_to_chat(self, body, resolved_model):
        from ..openai import store
        from ..openai.transform import responses_to_chat
        guard.guard_responses_to_chat(body, store_enabled=store.is_enabled())
        key = str(body.get("_api_key_name") or "")
        items = responses_to_chat.resolve_input_items(body, api_key_name=key)
        payload = responses_to_chat.translate_request_from_input_items(body, items)
        payload["messages"] = responses_to_chat._instructions_to_messages(body.get("instructions")) + workbuddy_replay.responses_messages(items)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(self, payload, protocol=self.protocol)
        return UpstreamRequest(url=self.base_url + self.api_path, headers={}, body=json.dumps(payload).encode(), translator_ctx={
            "ingress": "responses", "upstream_protocol": self.protocol, "response_translator": "responses_to_chat",
            "model_for_response": resolved_model, "previous_response_id": body.get("previous_response_id"),
            "api_key_name": key, "channel_key": self.key,
            "current_input_items": responses_to_chat.resolve_current_input_items(body), "request_body": body})

    async def build_upstream_request(self, requested_body, resolved_model, *, ingress_protocol="anthropic"):
        current = oauth_manager.get_account(self.account_key)
        if not current or resolved_model not in oauth_manager.account_model_selection(current)["effective_models"]:
            raise guard.GuardError(400, "invalid_request_error", "WorkBuddy model unavailable for this account", param="model", scope="candidate")
        requested_body = workbuddy_context.ensure_context(requested_body)
        request = await super().build_upstream_request(copy.deepcopy(requested_body), resolved_model, ingress_protocol=ingress_protocol)
        payload = json.loads(request.body)
        workbuddy_replay.restore_chat_fields(payload)
        if ingress_protocol != "anthropic" or not workbuddy_replay.has_unreplayed_anthropic_thinking(requested_body.get("messages") or []):
            merge_tool_call_messages(payload)
        if payload.get("n", 1) not in (None, 1):
            raise guard.GuardError(400, "invalid_request_error", "WorkBuddy supports one completion per request", param="n", scope="candidate")
        if payload.get("functions") or payload.get("function_call"):
            raise guard.GuardError(400, "invalid_request_error", "Use tools/tool_choice with WorkBuddy", param="functions", scope="candidate")
        catalog = current.get("account_model_catalog") or {}
        records = catalog.get("models") or []
        record = next((r for r in records if isinstance(r, dict) and r.get("id") == resolved_model), {})
        efforts = record.get("reasoningEfforts") or []
        modern = not common.is_legacy(current)
        if modern and ingress_protocol == "anthropic" and (requested_body.get("thinking") or {}).get("type") == "disabled":
            payload["reasoning_effort"] = "none"
        effort = payload.get("reasoning_effort")
        if modern and effort in ("none", "off"):
            if record.get("canDisableThinking") is not True:
                raise guard.GuardError(400, "invalid_request_error", "WorkBuddy model does not declare disabling thinking support", param="reasoning_effort", scope="candidate")
            payload.pop("reasoning_effort", None)
            payload.pop("reasoning_summary", None)
        elif effort is not None and efforts and effort not in efforts:
            raise guard.GuardError(400, "invalid_request_error", "Requested reasoning_effort is not supported by this WorkBuddy model", param="reasoning_effort", scope="candidate")
        payload["model"] = record.get("wireModel") or resolved_model
        if record.get("supportsToolCall") is False and payload.get("tools"):
            raise guard.GuardError(400, "invalid_request_error", "WorkBuddy model does not support tools", param="tools", scope="candidate")
        normalize_tool_choice(payload)
        # All ingress protocols have converged to Chat here. Keep adaptation
        # provider/profile-local and read the current config on every dispatch.
        if common.realm_of(current) == "cn" and common.profile_of(current) == common.PROFILE:
            payload, hits = rewrite_payload(payload, settings_from_config(config.get()))
            for rule_id, count in hits.items():
                _logger.debug("WorkBuddy request rewrite rule=%s hits=%d", rule_id, count)
        if common.profile_of(current) == common.WORKBUDDY_CLI_PROFILE:
            payload["cache_retention"] = "long"
            for field in ("metadata", "conversation_id", "previous_response_id"):
                payload.pop(field, None)
        add_client_identity(payload)
        payload["stream"] = True
        payload["stream_options"] = {**(payload.get("stream_options") or {}), "include_usage": True}
        token = await oauth_manager.ensure_channel_token(self)
        current = oauth_manager.get_account(self.account_key)
        if not current:
            raise common.WorkBuddyError("chat", kind="stale_generation")
        headers = common.headers(dict(current, access_token=token), "chat")
        headers["Accept"] = "text/event-stream"
        if not common.is_legacy(current):
            ide = common.profile_of(current) == common.GLOBAL_PROFILE
            headers.update(workbuddy_context.headers(requested_body, self.account_key, ide=ide,
                workbuddy_cli=common.profile_of(current) == common.WORKBUDDY_CLI_PROFILE))
            if ide:
                headers["X-Model-ID"] = resolved_model
            headers["X-Private-Data"] = "true"
        request.url = common.api_base_url(current) + "/v2/chat/completions"
        request.headers = headers
        request.body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        # Older last-good catalogs predate this field. Modern products default
        # to gzip; an explicit remote false still wins, and legacy never opts in.
        # workbuddy.ai is outside officialEndpoints: official CODEBUDDY_BASE_URL
        # routing skips gzip regardless of product/cloud RequestBodyGzip.
        if (not common.is_legacy(current) and common.profile_of(current) != common.WORKBUDDY_CLI_PROFILE
                and record.get("requestBodyGzip", True) is True):
            request.body = gzip.compress(request.body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        request.translator_ctx = model_names.response_context(
            self.provider, resolved_model, request.translator_ctx, ingress=ingress_protocol,
        )
        request.translator_ctx = {**(request.translator_ctx or {}), "workbuddy_stream": WorkBuddyStream(single_choice=ingress_protocol != "chat"), "workbuddy_replay": True,
            "model_for_response": model_names.public_id(self.provider, resolved_model),
            "response_model_override": model_names.public_id(self.provider, resolved_model)}
        if ingress_protocol == "chat":
            request.translator_ctx.update(response_translator="chat_model_alias", ingress="chat", upstream_protocol=self.protocol)
        request.dispatch_metadata = build_dispatch_metadata(payload, self.protocol, headers)
        return request

    async def restore_response(self, chunk, dynamic_map=None):
        return chunk

    def display(self):
        return ChannelDisplay(self.key, self.type, self.display_name, self.enabled, self.disabled_reason, self.list_client_models())
