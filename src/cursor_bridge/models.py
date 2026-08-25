"""Discover Cursor models via AvailableModels."""

from __future__ import annotations

from dataclasses import dataclass

from . import aiserver_pb2
from .connect import decode_connect_unary_body
from .constants import AVAILABLE_MODELS_PATH, DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_TOKENS
from .h2stream import unary_rpc


@dataclass(frozen=True)
class CursorModel:
    id: str
    name: str
    reasoning: bool
    context_window: int
    max_tokens: int
    supports_images: bool
    supports_max_mode: bool
    context_window_max_mode: int | None = None
    legacy_slugs: tuple[str, ...] = ()
    server_model_name: str | None = None
    tagline: str | None = None
    supports_agent: bool = False
    supports_plan_mode: bool = False
    is_long_context_only: bool = False
    default_on: bool = False
    price: float | None = None
    id_aliases: tuple[str, ...] = ()

    def to_openai(self) -> dict[str, object]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "cursor",
            "name": self.name,
            "reasoning": self.reasoning,
            "context_window": self.context_window,
            "max_tokens": self.max_tokens,
            "supports_images": self.supports_images,
            "supports_max_mode": self.supports_max_mode,
            "context_window_max_mode": self.context_window_max_mode,
            "legacy_slugs": list(self.legacy_slugs),
            "server_model_name": self.server_model_name,
            "tagline": self.tagline,
            "supports_agent": self.supports_agent,
            "supports_plan_mode": self.supports_plan_mode,
            "is_long_context_only": self.is_long_context_only,
            "default_on": self.default_on,
            "price": self.price,
            "id_aliases": list(self.id_aliases),
        }


def _decode_models(payload: bytes) -> aiserver_pb2.AvailableModelsResponse | None:
    response = aiserver_pb2.AvailableModelsResponse()
    candidates = [payload]
    framed = decode_connect_unary_body(payload)
    if framed is not None:
        candidates.append(framed)
    for blob in candidates:
        try:
            parsed = aiserver_pb2.AvailableModelsResponse()
            parsed.ParseFromString(blob)
            if parsed.models or parsed.model_names:
                return parsed
            response = parsed
        except Exception:
            continue
    return response if response.ByteSize() else None


def list_cursor_models(
    access_token: str,
    *,
    include_hidden: bool = False,
    use_model_parameters: bool = True,
    timeout_s: float | None = None,
    account_key: str = "",
    channel_key: str = "",
) -> list[CursorModel]:
    """Return the account-specific canonical Cursor model catalog.

    ``use_model_parameters=true`` collapses hundreds of effort/fast/thinking
    wire variants into canonical models and exposes the real ids through each
    model's ``legacy_slugs``.  Parrot stores those slugs for request-time
    resolution but publishes only the canonical ids downstream.
    """
    request = aiserver_pb2.AvailableModelsRequest(
        include_long_context_models=True,
        include_hidden_models=include_hidden,
        use_model_parameters=use_model_parameters,
    )
    rpc_kwargs = {"timeout_s": max(0.001, float(timeout_s))} if timeout_s is not None else {}
    payload = unary_rpc(
        AVAILABLE_MODELS_PATH,
        access_token,
        request.SerializeToString(),
        account_key=account_key,
        channel_key=channel_key,
        purpose="oauth_cursor",
        **rpc_kwargs,
    )
    decoded = _decode_models(payload)
    if decoded is None:
        return []
    models: list[CursorModel] = []
    for item in decoded.models:
        model_id = item.name.strip()
        if (
            not model_id
            or (item.is_hidden and not include_hidden)
            or item.is_chat_only
            or item.only_supports_cmd_k
            or not item.supports_agent
        ):
            continue
        models.append(
            CursorModel(
                id=model_id,
                name=(item.client_display_name or model_id).strip() or model_id,
                reasoning=bool(item.supports_thinking),
                context_window=item.context_token_limit or DEFAULT_CONTEXT_WINDOW,
                context_window_max_mode=item.context_token_limit_for_max_mode or None,
                max_tokens=DEFAULT_MAX_TOKENS,
                supports_images=bool(item.supports_images),
                supports_max_mode=bool(item.supports_max_mode),
                legacy_slugs=tuple(item.legacy_slugs),
                server_model_name=(item.server_model_name or "").strip() or None,
                tagline=(item.tagline or "").strip() or None,
                supports_agent=bool(item.supports_agent),
                supports_plan_mode=bool(item.supports_plan_mode),
                is_long_context_only=bool(item.is_long_context_only),
                default_on=bool(item.default_on),
                price=item.price if item.HasField("price") else None,
                id_aliases=tuple(item.id_aliases),
            )
        )
    models.sort(key=lambda model: model.id)
    return models
