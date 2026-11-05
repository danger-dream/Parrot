"""Cursor OAuth channel backed by Parrot's private AgentService bridge."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .. import cache_hints, json_bytes, oauth_manager
from ..cursor_bridge import catalog as cursor_catalog
from .. import model_names
from ..cursor_bridge import runtime as cursor_runtime
from ..openai.channel.api_channel import OpenAIApiChannel
from ..openai.transform import anthropic_to_chat, guard
from ..providers import registry as provider_registry
from ..transform import cc_mimicry
from .base import ChannelDisplay, UpstreamRequest, build_dispatch_metadata
from .compatibility import apply_reasoning_effort_capability
from .oauth_helpers import request_api_key_name as _request_api_key_name


def _contains_image(payload: dict) -> bool:
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"image", "image_url", "input_image"}:
                return True
    return False


def _thinking_override(source: dict, payload: dict) -> bool | None:
    thinking = source.get("thinking")
    if isinstance(thinking, dict):
        typ = str(thinking.get("type") or "").strip().lower()
        if typ == "disabled":
            return False
        if typ in {"enabled", "adaptive"}:
            return True
    effort = cursor_catalog.normalize_effort(payload.get("reasoning_effort"))
    if effort in {"none", "minimal"}:
        return False
    return True if effort else None


class CursorOAuthChannel(OpenAIApiChannel):
    """One Cursor OAuth account exposed as an OpenAI Chat-family channel.

    OpenAI Responses and Anthropic Messages ingress are translated by Parrot's
    existing bridges.  The actual Cursor model variant is selected only after
    those translations expose reasoning/fast controls in Chat form.
    """

    type = "oauth"
    provider = "cursor"
    protocol = "openai-chat"
    cc_mimicry = False
    upstream_stream_only = False
    internal_loopback = True

    def __init__(self, account: dict):
        from ..oauth_ids import account_key as _account_key

        self.account = dict(account)
        self.email = str(account.get("email") or "Cursor")
        self.subject = str(account.get("subject") or account.get("sub") or "")
        self.account_key = _account_key(account)
        self.key = f"oauth:{self.account_key}"
        self.display_name = str(account.get("label") or self.email)
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")
        self.plan_type = str(account.get("plan_type") or "")
        try:
            self.max_concurrent = int(account.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0

        self._records = cursor_catalog.catalog_records(account)
        self._disabled_models = oauth_manager.cursor_disabled_models(account)
        self._max_context_defaults = oauth_manager.cursor_max_context_models(account)
        # Cursor has no trustworthy static/legacy model fallback. Route only
        # IDs present in both the last successful model list and native catalog;
        # either side missing/stale stays fail-closed until discovery succeeds.
        selected_models = {
            str(item).strip() for item in account.get("models") or [] if str(item).strip()
        }
        catalog_models = [
            str(item.get("id") or "") for item in self._records if item.get("id")
        ]
        models = [
            model for model in catalog_models
            if model in selected_models and model not in self._disabled_models
        ]
        self.models = list(dict.fromkeys(models))

        # Reuse the mature OpenAI Chat request/response implementation while
        # replacing only identity, model resolution, and internal transport.
        super().__init__({
            "name": self.display_name,
            "baseUrl": cursor_runtime.base_url(),
            "apiKey": cursor_runtime.bearer_secret(),
            "protocol": "openai-chat",
            "models": [{"real": model, "alias": model} for model in self.models],
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "omitTemperature": False,
            "omitThinking": False,
        })
        # super().__init__ describes an API channel; restore OAuth identity.
        self.type = "oauth"
        self.provider = "cursor"
        self.account_key = _account_key(account)
        self.key = f"oauth:{self.account_key}"
        self.email = str(account.get("email") or "Cursor")
        self.display_name = str(account.get("label") or self.email)
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")
        try:
            self.max_concurrent = int(account.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0
        self._records = cursor_catalog.catalog_records(account)
        self._disabled_models = oauth_manager.cursor_disabled_models(account)
        self._max_context_defaults = oauth_manager.cursor_max_context_models(account)
        self.models = list(dict.fromkeys(models))

    def _record(self, model: str) -> dict[str, Any] | None:
        for item in self._records:
            if str(item.get("id") or "") == model:
                return item
        return None

    def supports_model(self, requested_model: str) -> Optional[str]:
        real = model_names.upstream_id(self.provider, requested_model)
        return real if real in self.models else None

    def list_client_models(self) -> list[str]:
        return [model_names.public_id(self.provider, model) for model in self.models]

    def cursor_metadata(self, model: str) -> dict[str, Any]:
        return cursor_catalog.metadata_from_record(self._record(model))

    @staticmethod
    def _context_limits(record: dict[str, Any] | None) -> tuple[int, int]:
        source = record or {}
        normal = int(source.get("context_window") or 0)
        maximum = int(source.get("context_window_max_mode") or normal or 0)
        return normal, maximum

    def has_separate_max_context(self, model: str) -> bool:
        normal, maximum = self._context_limits(self._record(model))
        return maximum > normal > 0

    def uses_max_context(self, body: dict, model: str) -> bool:
        """Resolve downstream tri-state over the persisted account/model default."""
        if not self.has_separate_max_context(model):
            return False
        override = body.get(cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY)
        if isinstance(override, bool):
            return override
        return model in self._max_context_defaults

    async def _build_anthropic_cursor_chat(self, body: dict, resolved_model: str) -> UpstreamRequest:
        # target_model=None avoids applying generic OpenAI-only model-name
        # heuristics. Cursor's own catalog/variant resolver is authoritative.
        payload = anthropic_to_chat.translate_request(body, target_model=None)
        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(self, payload, protocol="openai-chat")
        cache_hints.apply_anthropic_cache_to_openai_payload(
            body,
            payload,
            model=resolved_model,
            api_key_name=body.get("_parrot_api_key_name"),
            client_ip=body.get("_parrot_client_ip"),
        )
        headers = self._headers()
        return UpstreamRequest(
            url=f"{self.base_url}/v1/chat/completions",
            headers=headers,
            body=json_bytes.dumps_utf8(payload, separators=(",", ":")),
            dynamic_tool_map=None,
            translator_ctx={
                "ingress": "anthropic",
                "upstream_protocol": "openai-chat",
                "response_translator": "anthropic_to_chat",
                "model_for_response": resolved_model,
            },
            dispatch_metadata=build_dispatch_metadata(payload, "openai-chat", headers),
        )

    async def build_upstream_request(
        self,
        requested_body: dict,
        resolved_model: str,
        *,
        ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        if resolved_model not in self.models:
            raise guard.GuardError(
                400,
                "invalid_request_error",
                f"Cursor model is disabled or unavailable for this account: {resolved_model!r}",
                param="model",
                scope="candidate",
            )
        access_token = await oauth_manager.ensure_channel_token(self)
        cursor_runtime.update_account(self.account_key, access_token)

        if ingress_protocol == "anthropic":
            request = await self._build_anthropic_cursor_chat(requested_body, resolved_model)
        else:
            request = await super().build_upstream_request(
                requested_body,
                resolved_model,
                ingress_protocol=ingress_protocol,
            )

        try:
            payload = json.loads(request.body)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise guard.GuardError(
                500,
                "server_error",
                f"Cursor request serialization failed: {exc}",
                scope="candidate",
            ) from exc
        if not isinstance(payload, dict):
            raise guard.GuardError(500, "server_error", "Cursor request payload is not an object", scope="candidate")

        record = self._record(resolved_model)
        if record is None:
            raise guard.GuardError(
                400,
                "invalid_request_error",
                f"Cursor account no longer exposes model {resolved_model!r}",
                param="model",
                scope="candidate",
            )
        response_format = payload.get("response_format")
        if response_format not in (None, {"type": "text"}):
            raise guard.GuardError(
                400,
                "invalid_request_error",
                "Cursor AgentService does not provide Chat response_format guarantees",
                param="response_format",
                scope="candidate",
            )
        if payload.get("functions") or payload.get("function_call"):
            raise guard.GuardError(
                400,
                "invalid_request_error",
                "Cursor bridge supports tools/tool_choice, not legacy functions/function_call",
                param="functions",
                scope="candidate",
            )
        # The imported AgentService bridge currently has no SelectedImage
        # transport. Reject instead of silently dropping image blocks.
        if _contains_image(payload):
            raise guard.GuardError(
                400,
                "invalid_request_error",
                "Cursor bridge image input is not available yet",
                param="messages",
                scope="candidate",
            )

        # Generic protocol translation preserves explicit max. Only here, with
        # the concrete Cursor account model and AvailableModels-derived effort
        # metadata known, may an xhigh-only model adapt max to xhigh.
        apply_reasoning_effort_capability(
            payload,
            record.get("reasoning_efforts"),
            protocol="openai-chat",
        )

        service_tier = str(payload.get("service_tier") or "").strip().lower()
        wants_fast = service_tier in {"priority", "fast"} or requested_body.get("_parrot_wants_fast_mode") is True
        thinking = _thinking_override(requested_body, payload)
        effort = payload.get("reasoning_effort")
        if (
            resolved_model.startswith("claude-")
            and effort is None
            and thinking is None
            and requested_body.get("thinking") is None
            and not wants_fast
        ):
            # Canonical Claude ids may not resolve without a Cursor preset.
            # Match explicit high using catalog slugs, without changing other
            # providers/models or overriding explicit thinking/fast controls.
            effort = "high"
            payload["reasoning_effort"] = effort
        if not cursor_catalog.normalize_effort(effort) and (wants_fast or thinking is not None):
            available = [str(item) for item in record.get("reasoning_efforts") or []]
            effort = "medium" if "medium" in available else "high" if "high" in available else None
            if wants_fast and thinking is None:
                # The effort was synthesized only to reach a fast slug; it must
                # not implicitly turn on a separate Claude thinking variant.
                thinking = False
        actual_model = cursor_catalog.resolve_variant(
            record,
            reasoning_effort=effort,
            fast=wants_fast,
            thinking=thinking,
        ) or resolved_model
        payload["model"] = actual_model

        override = requested_body.get(cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY)
        normal_context, max_context = self._context_limits(record)
        if (
            override is True
            and normal_context < cc_mimicry.ONE_M_CONTEXT_TOKENS
            and max_context <= normal_context
        ):
            raise guard.GuardError(
                400,
                "invalid_request_error",
                f"Cursor model {resolved_model!r} does not expose a separate Max Context tier",
                param="model",
                scope="candidate",
            )
        wants_long = self.uses_max_context(requested_body, resolved_model)
        if wants_long:
            payload["cursor_long_context"] = True

        # Standard clients can supply a stable cache/user key; tool-result turns
        # are independently pinned by tool_call_id inside the bridge runtime.
        anchor = str(payload.get("prompt_cache_key") or payload.get("user") or "").strip()
        if anchor:
            material = f"{_request_api_key_name(requested_body)}:{anchor}".encode("utf-8")
            request.headers[cursor_runtime._SESSION_HEADER] = hashlib.sha256(material).hexdigest()
        # Principal is a trusted routing fact, independent of user/cache anchors.
        request.headers[cursor_runtime._PRINCIPAL_HEADER] = hashlib.sha256(
            _request_api_key_name(requested_body).encode("utf-8")
        ).hexdigest()
        request.headers[cursor_runtime._ACCOUNT_HEADER] = self.account_key
        request.headers["Authorization"] = f"Bearer {cursor_runtime.bearer_secret()}"
        request.body = json_bytes.dumps_utf8(payload, separators=(",", ":"))
        request.dispatch_metadata = build_dispatch_metadata(
            payload, "openai-chat", request.headers,
        )
        ctx = dict(request.translator_ctx or {})
        ctx.update({
            "cursor_client_model": resolved_model,
            "cursor_actual_model": actual_model,
            "cursor_long_context": bool(payload.get("cursor_long_context")),
        })
        request.translator_ctx = model_names.response_context(
            self.provider, resolved_model, ctx, ingress=ingress_protocol,
        )
        return request

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cursor_runtime.bearer_secret()}",
            cursor_runtime._ACCOUNT_HEADER: self.account_key,
            "User-Agent": "parrot/cursor-oauth-adapter",
        }

    def display(self) -> ChannelDisplay:
        return ChannelDisplay(
            key=self.key,
            type="oauth",
            display_name=self.display_name,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            models=self.list_client_models(),
        )
