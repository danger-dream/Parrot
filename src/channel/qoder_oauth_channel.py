"""Qoder OAuth as a stream-only OpenAI Chat channel over Qoder's own agent protocol."""
from __future__ import annotations

import copy
import json

from .. import model_names, oauth_manager
from ..oauth.qoder import common, catalog, request as qoder_request
from ..oauth_ids import account_key
from ..openai.channel.api_channel import OpenAIApiChannel
from ..openai.transform import guard
from ..providers.qoder_codec import QoderStream, QoderWire
from .base import ChannelDisplay, build_dispatch_metadata


class QoderOAuthChannel(OpenAIApiChannel):
    type = "oauth"
    provider = "qoder"
    protocol = "openai-chat"
    cc_mimicry = False
    upstream_stream_only = True

    def __init__(self, account: dict):
        selection = oauth_manager.account_model_selection(account)
        models = selection["effective_models"]
        aliases = catalog.model_aliases(selection["records"])
        name = str(account.get("label") or account.get("email") or account["uid"])
        super().__init__({"name": name, "baseUrl": common.site_of(account)["api"],
            "apiPath": "/algo/api/v2/service/pro/sse/agent_chat_generation", "protocol": "openai-chat",
            "models": [{"real": model, "alias": aliases.get(model, model)} for model in models],
            "enabled": account.get("enabled", True), "disabled_reason": account.get("disabled_reason"),
            "responsesWsUpstreamTransport": "sse"})
        self.account = copy.deepcopy(account)
        self.account_key = account_key(account)
        self.key = "oauth:" + self.account_key
        self.email = str(account.get("email") or name)
        self.type, self.provider = "oauth", "qoder"
        try:
            self.max_concurrent = max(0, int(account.get("maxConcurrent") or 0))
        except (ValueError, TypeError):
            self.max_concurrent = 0

    def supports_model(self, requested_model: str):
        if any(item.get("real") == requested_model for item in self.models):
            return requested_model
        return super().supports_model(requested_model)

    def _is_deepseek(self, model=None) -> bool:
        # Qoder serves every model on its own protocol; no vendor defaults apply.
        return False

    def _apply_compatibility(self, payload, resolved_model, **_context):
        # Effort is fitted to the model's own Qoder levels when the body is written.
        pass

    async def build_upstream_request(self, requested_body, resolved_model, *, ingress_protocol="anthropic"):
        unsupported = {"stop", "stop_sequences", "top_p", "top_k", "seed",
                       "frequency_penalty", "presence_penalty", "logit_bias", "logprobs", "top_logprobs",
                       "parallel_tool_calls"}
        for field in unsupported:
            if field in requested_body and requested_body[field] is not None:
                raise guard.GuardError(400, "invalid_request_error", f"Qoder does not support {field}", param=field, scope="candidate")
        response_format = requested_body.get("response_format")
        if response_format is not None and response_format != {"type": "text"}:
            raise guard.GuardError(400, "invalid_request_error", "Qoder does not support structured output", param="response_format", scope="candidate")
        if (isinstance(requested_body.get("text"), dict) and requested_body["text"].get("format")
                and requested_body["text"]["format"] != {"type": "text"}):
            raise guard.GuardError(400, "invalid_request_error", "Qoder does not support structured output", param="text.format", scope="candidate")
        choice = requested_body.get("tool_choice")
        if choice is not None and choice not in ("auto", "none") and not (
                isinstance(choice, dict) and choice.get("type") in {"auto", "none"}):
            raise guard.GuardError(400, "invalid_request_error", "Qoder cannot enforce required or named tool choice", param="tool_choice", scope="candidate")
        current = oauth_manager.get_account(self.account_key)
        if not current or resolved_model not in oauth_manager.account_model_selection(current)["effective_models"]:
            raise guard.GuardError(400, "invalid_request_error", "Qoder model unavailable for this account", param="model", scope="candidate")
        record = next((r for r in oauth_manager.account_model_records(current) if r.get("id") == resolved_model), None)
        if not record or not isinstance(record.get("qoderConfig"), dict):
            raise guard.GuardError(400, "invalid_request_error", "Qoder model has no synced configuration; sync this account's models",
                                   param="model", scope="candidate")
        request = await super().build_upstream_request(copy.deepcopy(requested_body), resolved_model, ingress_protocol=ingress_protocol)
        payload = json.loads(request.body)
        if payload.get("n", 1) not in (None, 1):
            raise guard.GuardError(400, "invalid_request_error", "Qoder supports one completion per request", param="n", scope="candidate")
        if ingress_protocol == "anthropic" and (requested_body.get("thinking") or {}).get("type") == "disabled":
            payload["reasoning_effort"] = "none"
        if "image" not in (record.get("inputModalities") or []) and any(
                block.get("type") == "image_url" for message in payload.get("messages") or [] if isinstance(message, dict)
                for block in (message.get("content") if isinstance(message.get("content"), list) else []) if isinstance(block, dict)):
            raise guard.GuardError(400, "invalid_request_error", "Qoder model does not accept images", param="messages", scope="candidate")
        payload["model"] = resolved_model
        body = qoder_request.body_for(payload, record)
        token = await oauth_manager.ensure_channel_token(self)
        current = oauth_manager.get_account(self.account_key)
        if not current:
            raise common.QoderError("chat", kind="stale_generation")
        signer = dict(current, access_token=token)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "Cache-Control": "no-cache",
                   "X-Model-Key": resolved_model, "X-Model-Source": str(record.get("qoderModelSource") or "")}
        request.url = common.site_of(current)["api"] + common.CHAT_PATH
        request.headers = headers
        request.body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        request.stream_context_hook = lambda physical, factory: QoderWire(physical, factory, signer, self.account_key)
        public = model_names.public_id(self.provider, resolved_model)
        requested = requested_body.get("model")
        if self.supports_model(requested) == resolved_model:
            public = requested
        request.translator_ctx = model_names.response_context(self.provider, resolved_model, request.translator_ctx, ingress=ingress_protocol)
        request.translator_ctx = {**(request.translator_ctx or {}), "qoder_stream": QoderStream(public), "model_for_response": public}
        if ingress_protocol == "chat":
            request.translator_ctx.setdefault("response_translator", "chat_model_alias")
            request.translator_ctx.update(ingress="chat", upstream_protocol=self.protocol, response_model_override=public)
        request.dispatch_metadata = build_dispatch_metadata(payload, self.protocol, headers)
        return request

    async def restore_response(self, chunk, dynamic_map=None):
        return chunk

    def display(self):
        return ChannelDisplay(self.key, self.type, self.display_name, self.enabled, self.disabled_reason, self.list_client_models())
