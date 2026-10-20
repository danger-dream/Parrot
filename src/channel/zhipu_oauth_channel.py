"""Coding Plan Anthropic channel; ZCode attribution without Claude/Agent rewrites."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid

from .. import config, model_names, oauth_manager
from ..oauth.zhipu import common as c, request_context, runtime, signing, project_tokens
from ..oauth.zhipu.response import BusinessContext, BusinessStream
from ..oauth_ids import account_key
from ..openai import anthropic_reasoning_replay
from ..openai.transform import chat_to_anthropic, responses_to_anthropic, guard
from ..providers import registry as providers
from .api_channel import ApiChannel
from .base import ChannelDisplay, UpstreamRequest, build_dispatch_metadata


class ZhipuOAuthChannel(ApiChannel):
    type = "oauth"
    provider = "zhipu"
    cc_mimicry = False
    protocol = "anthropic"

    def __init__(self, account):
        name = account.get("label") or account.get("subject") or "Zhipu"
        models = oauth_manager.account_model_selection(account)["effective_models"]
        super().__init__({"name": name, "baseUrl": c.MODEL_ORIGINS[c.site_of(account)],
            "apiPath": "/api/anthropic/v1/messages", "cc_mimicry": False,
            "models": [{"real": m, "alias": m} for m in models],
            "enabled": account.get("enabled", True), "disabled_reason": account.get("disabled_reason"),
            "maxConcurrent": account.get("maxConcurrent", 0)})
        self.account = copy.deepcopy(account)
        self.account_key = account_key(account)
        self.key = "oauth:" + self.account_key
        self.email = name

    def supports_model(self, requested_model):
        return super().supports_model(model_names.canonical_id(self.provider, requested_model))

    async def build_upstream_request(self, requested_body, resolved_model, *, ingress_protocol="anthropic"):
        resolved_model = model_names.canonical_id(self.provider, resolved_model)
        token = await oauth_manager.ensure_channel_token(self)
        current = oauth_manager.get_account(self.account_key)
        if current:
            # ensure_channel_token awaits IO. Do not attach its old token to a
            # login replaced between that await and this request snapshot.
            bound_token = (current.get("model_key") if current.get("credential_mode") == "api_key"
                           else project_tokens.cached_token(current))
            if bound_token != token:
                raise c.ZhipuError("model_request", "scope_invalidated")
        if not current or resolved_model not in oauth_manager.account_model_selection(current)["effective_models"]:
            raise guard.GuardError(400, "invalid_request_error", "Zhipu model unavailable for this account", param="model", scope="candidate")
        source = request_context.ensure_request_context(copy.deepcopy(requested_body))
        ctx = None
        if ingress_protocol == "chat":
            payload = chat_to_anthropic.translate_request(source)
            ctx = {"ingress": "chat", "upstream_protocol": "anthropic", "response_translator": "chat_to_anthropic",
                   "model_for_response": resolved_model, "include_usage": bool((source.get("stream_options") or {}).get("include_usage"))}
        elif ingress_protocol == "responses":
            from ..openai import store
            key_name = str(source.get("_api_key_name") or "")
            tool_map = responses_to_anthropic.NamespaceToolMap()
            items = responses_to_anthropic.resolve_current_input_items(source)
            payload = responses_to_anthropic.translate_request(source, api_key_name=key_name, store_enabled=store.is_enabled(), namespace_tool_map=tool_map, target_model=resolved_model, allow_unsigned_thinking=True)
            ctx = {"ingress": "responses", "upstream_protocol": "anthropic", "response_translator": "responses_to_anthropic",
                   "model_for_response": resolved_model, "previous_response_id": source.get("previous_response_id"),
                   "api_key_name": key_name, "channel_key": self.key, "current_input_items": items,
                   "request_body": source, "namespace_tool_map": tool_map, "allow_unsigned_thinking": True}
        elif ingress_protocol == "anthropic":
            payload = source
        else:
            raise ValueError("Unsupported Zhipu ingress")
        payload = providers.filter_request_payload(self, payload, protocol="anthropic", bridge=ctx is not None)
        record = next((r for r in oauth_manager.account_model_records(current) if r.get("id") == resolved_model), {})
        payload["model"] = record.get("upstreamId") or next(
            (model for model in current.get("models") or []
             if model_names.canonical_id(self.provider, model) == resolved_model), resolved_model)
        payload.setdefault("max_tokens", 4096)
        payload.setdefault("stream", False)
        # Explicit parameters are authoritative. Only adapt an explicitly chosen
        # reasoning level, never inject model-wide thinking/max token defaults.
        effort = source.get("reasoning_effort") if ingress_protocol == "chat" else (source.get("reasoning") or {}).get("effort") if ingress_protocol == "responses" else None
        if effort is not None:
            values = record.get("reasoningEfforts") or []
            if values and effort not in values:
                raise guard.GuardError(400, "invalid_request_error", "Unsupported Zhipu reasoning effort", param="reasoning_effort", scope="candidate")
            payload["thinking"] = {"type": "disabled" if effort in {"disabled", "none"} else "enabled"}
            if effort not in {"disabled", "none", "enabled"}:
                payload["output_config"] = {**(payload.get("output_config") or {}), "effort": effort}
        if source.get(anthropic_reasoning_replay.DROP_HISTORY_KEY):
            anthropic_reasoning_replay.drop_thinking_history(payload, allow_unsigned_thinking=True)
        else:
            anthropic_reasoning_replay.drop_known_rejected_thinking(
                payload, self.key, allow_unsigned_thinking=True,
            )
        session = source["_parrot_zcode_session"]
        device_id = await asyncio.to_thread(runtime.ensure_device_id, self.account_key, current)
        request_context.add_metadata(payload, device_id=device_id, session_id=session)
        headers = {**c.model_headers(), "Content-Type": "application/json", "anthropic-version": "2023-06-01",
            "x-api-key": token, "Authorization": "Bearer " + token, "x-session-id": session,
            "x-zcode-trace-id": source["_parrot_zcode_trace"], "x-query-id": source["_parrot_zcode_query"],
            "x-request-id": str(uuid.uuid4()), "x-zcode-session-type": "main"}
        if request_context.needs_mid_system_beta(payload):
            headers["anthropic-beta"] = request_context.MID_SYSTEM_BETA
        if current.get("plan_scope") == "team":
            headers.update(c.scope_headers(current))
        cfg = config.get()
        network_revision = hashlib.sha256(json.dumps({k: cfg.get(k) for k in ("network", "proxy", "proxies", "socks5")}, sort_keys=True).encode()).hexdigest()
        signer = signing.for_account(dict(current, model_key=token), self.account_key, network_revision=network_revision)
        observation = {"resolved": True, "scope": project_tokens.scope(current),
            "state_key": oauth_manager.account_state_key(current),
            "fingerprint": project_tokens.token_fingerprint(token), "mode": current.get("credential_mode")}
        ctx = {**(ctx or {}), "zhipu_stream": BusinessStream(), "zhipu_auth": observation}
        def wire_context(request, factory):
            return BusinessContext(signer.wrap(request, factory), observation)
        return UpstreamRequest(c.MODEL_ORIGINS[c.site_of(current)] + "/api/anthropic/v1/messages", headers,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), translator_ctx=ctx,
            dispatch_metadata=build_dispatch_metadata(payload, "anthropic", headers), stream_context_hook=wire_context)

    def display(self):
        return ChannelDisplay(self.key, self.type, self.display_name, self.enabled, self.disabled_reason, self.list_client_models())
