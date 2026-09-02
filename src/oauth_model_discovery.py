"""Account-scoped OAuth model discovery adapters.

The adapters only perform and validate upstream catalog requests. Persistence,
LKG selection and single-flight orchestration live in :mod:`oauth_manager`.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from . import network
from .oauth import antigravity as antigravity_provider
from .oauth import cursor as cursor_provider
from .oauth import xai as xai_provider
from .oauth_ids import openai_workspace_id
from .openai.codex_constants import (
    CODEX_ORIGINATOR,
    build_codex_cli_user_agent,
    codex_cli_version,
)

_TIMEOUT = 20.0


class _Deadline:
    """One monotonic request budget shared by every step of an adapter."""

    def __init__(self, timeout: float):
        self.end = time.monotonic() + max(0.001, float(timeout))

    def remaining(self) -> float:
        value = self.end - time.monotonic()
        if value <= 0:
            raise TimeoutError("OAuth model discovery deadline exhausted")
        return value


@dataclass(frozen=True)
class DiscoveryResult:
    models: list[str]
    catalog: dict[str, Any]
    source: str


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()
    ))


def _json_object(response: Any) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("model catalog response must be an object")
    return payload



def _positive(value: Any) -> int | None:
    try: parsed = int(value)
    except (TypeError, ValueError): return None
    return parsed if parsed > 0 else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str): value = [value]
    return _unique(value) if isinstance(value, list) else []


def _service_tiers(value: Any) -> list[dict[str, str]]:
    """Keep only public service-tier capability fields from an account catalog."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        tier_id = str(raw.get("id") or "").strip().lower()
        if not tier_id or tier_id in seen:
            continue
        item = {"id": tier_id}
        for key in ("name", "description"):
            text = str(raw.get(key) or "").strip()
            if text:
                item[key] = text
        result.append(item)
        seen.add(tier_id)
    return result


def _record(model_id: Any, raw: dict, mapping: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Build the small provider-neutral persisted UI record (allow-list only)."""
    result: dict[str, Any] = {"id": str(model_id).strip()}
    for target, sources in mapping.items():
        value = next((raw.get(key) for key in sources if raw.get(key) is not None), None)
        if value is None: continue
        if target in {"contextWindow", "contextWindowMaxMode", "maxOutputTokens"}:
            value = _positive(value)
            if value is None: continue
        elif target == "serviceTiers":
            if not isinstance(value, list):
                continue
            # Preserve an explicit empty list: for a successful authenticated
            # account catalog it means the model advertised no service tier.
            value = _service_tiers(value)
        elif target in {
            "inputModalities", "outputModalities", "reasoningEfforts", "aliases",
            "additionalSpeedTiers",
        }:
            value = _strings(value)
            if not value: continue
        elif target in {"reasoning", "supportsImages", "supportsThinking"}:
            if not isinstance(value, bool): continue
        elif isinstance(value, str):
            value = value.strip()
            if not value: continue
        result[target] = value
    return result


def _catalog(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": 1, "models": records}


def discover_openai(account: dict, *, timeout: float = _TIMEOUT, proxy_channel: str = "") -> DiscoveryResult:
    deadline = _Deadline(timeout)
    token = str(account.get("access_token") or "")
    if not token:
        raise ValueError("missing access token")
    client_version = codex_cli_version()
    url = (
        "https://chatgpt.com/backend-api/codex/models"
        f"?client_version={client_version}"
    )
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "user-agent": build_codex_cli_user_agent(client_version),
        "originator": CODEX_ORIGINATOR,
        "origin": "https://chatgpt.com",
    }
    workspace = openai_workspace_id(account)
    if workspace:
        headers["ChatGPT-Account-ID"] = workspace
    payload = _json_object(network.get_sync(
        url, headers=headers, timeout=deadline.remaining(), proxy_purpose="oauth_openai", proxy_channel=proxy_channel,
    ))
    records = payload.get("models")
    if not isinstance(records, list):
        raise ValueError("Codex model catalog has invalid models schema")
    # Official Codex uses visibility="list" / "hide". Boolean spellings are
    # retained only for older compatible payloads; unknown visibility is not routed.
    def is_listed(item: dict) -> bool:
        visibility = str(item.get("visibility") or "").strip().lower()
        if visibility:
            return visibility == "list"
        explicit = item.get("visible", item.get("is_visible", item.get("isVisible")))
        return explicit is True

    models = _unique([
        item.get("slug") for item in records
        if isinstance(item, dict) and item.get("slug") and is_listed(item)
    ])
    if not models:
        raise ValueError("Codex model catalog is empty")
    normalized = []
    for item in records:
        if not isinstance(item, dict) or str(item.get("slug") or "").strip() not in models: continue
        normalized.append(_record(item["slug"], item, {
            "name": ("display_name", "displayName", "name"), "description": ("description", "tagline"),
            "contextWindow": ("context_window", "contextWindow"),
            "contextWindowMaxMode": ("max_context_window", "context_window_max_mode", "contextWindowMaxMode"),
            "maxOutputTokens": ("max_output_tokens", "maxOutputTokens"),
            "inputModalities": ("input_modalities", "inputModalities"), "outputModalities": ("output_modalities", "outputModalities"),
            "reasoning": ("reasoning", "supports_reasoning"),
            "reasoningEfforts": ("supported_reasoning_levels", "reasoning_levels", "reasoning_efforts", "reasoningEfforts"),
            "defaultReasoningEffort": ("default_reasoning_level", "defaultReasoningEffort"),
            "supportsImages": ("supports_images", "supportsImages"), "createdAt": ("created_at", "createdAt"),
            "serviceTiers": ("service_tiers", "serviceTiers"),
            "defaultServiceTier": ("default_service_tier", "defaultServiceTier"),
            "minimalClientVersion": ("minimal_client_version", "minimalClientVersion"),
            "additionalSpeedTiers": ("additional_speed_tiers", "additionalSpeedTiers"),
        }))
    return DiscoveryResult(models, _catalog(normalized), "upstream:codex")


def discover_claude(account: dict, *, timeout: float = _TIMEOUT, proxy_channel: str = "") -> DiscoveryResult:
    deadline = _Deadline(timeout)
    token = str(account.get("access_token") or "")
    if not token:
        raise ValueError("missing access token")
    url = "https://api.anthropic.com/v1/models?limit=1000"
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    }
    all_records: list[dict] = []
    seen_urls: set[str] = set()
    while url:
        if url in seen_urls:
            raise ValueError("Claude model catalog pagination loop")
        seen_urls.add(url)
        payload = _json_object(network.get_sync(
            url, headers=headers, timeout=deadline.remaining(), proxy_purpose="oauth_anthropic", proxy_channel=proxy_channel,
        ))
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Claude model catalog has invalid data schema")
        all_records.extend(item for item in data if isinstance(item, dict))
        if not payload.get("has_more"):
            break
        cursor = payload.get("last_id")
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("Claude model catalog is missing pagination cursor")
        from urllib.parse import quote
        url = "https://api.anthropic.com/v1/models?limit=1000&after_id=" + quote(cursor, safe="")
    models = _unique([item.get("id") for item in all_records if item.get("id")])
    if not models:
        raise ValueError("Claude model catalog is empty")
    normalized = []
    for item in all_records:
        model_id = item.get("id")
        if not model_id: continue
        caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        merged = dict(item); supported = caps.get("supported")
        if isinstance(supported, dict):
            if isinstance(supported.get("reasoning"), bool): merged["reasoning"] = supported["reasoning"]
            if isinstance(supported.get("vision"), bool): merged["supportsImages"] = supported["vision"]
        reasoning_cap, effort_cap, vision_cap = caps.get("reasoning"), caps.get("effort"), caps.get("vision")
        if isinstance(reasoning_cap, dict) and isinstance(reasoning_cap.get("supported"), bool):
            merged["reasoning"] = reasoning_cap["supported"]
        if isinstance(vision_cap, dict) and isinstance(vision_cap.get("supported"), bool):
            merged["supportsImages"] = vision_cap["supported"]
        efforts = caps.get("effort_levels", caps.get("reasoning_efforts"))
        if isinstance(effort_cap, dict):
            if effort_cap.get("supported") is True: merged["reasoning"] = True
            efforts = effort_cap.get("levels", effort_cap.get("values", efforts))
        if efforts is not None: merged["reasoningEfforts"] = efforts
        normalized.append(_record(model_id, merged, {
            "name": ("display_name", "displayName", "name"), "description": ("description",),
            "contextWindow": ("max_input_tokens", "context_window", "contextWindow"),
            "maxOutputTokens": ("max_tokens", "max_output_tokens", "maxOutputTokens"),
            "reasoning": ("reasoning",), "reasoningEfforts": ("reasoningEfforts",),
            "supportsImages": ("supportsImages",), "createdAt": ("created_at", "createdAt"),
        }))
    return DiscoveryResult(models, _catalog(normalized), "upstream:anthropic")


def _xai_is_text(record: dict) -> bool:
    # /language-models is already typed. For /models fallback require positive
    # structural evidence; never infer modality from names such as "imagine".
    kind = str(record.get("type") or record.get("model_type") or record.get("modality") or "").lower()
    if kind in {"language", "language_model", "text", "chat", "completion"}:
        return True
    capabilities = record.get("capabilities")
    if isinstance(capabilities, dict):
        return any(capabilities.get(key) is True for key in ("text", "chat", "completion", "responses"))
    if isinstance(capabilities, list):
        return bool({str(x).lower() for x in capabilities} & {"text", "chat", "completion", "responses"})
    return False


def discover_xai(account: dict, *, timeout: float = _TIMEOUT, proxy_channel: str = "") -> DiscoveryResult:
    deadline = _Deadline(timeout)
    token = str(account.get("access_token") or "")
    if not token: raise ValueError("missing access token")
    base = str(account.get("base_url") or account.get("baseUrl") or xai_provider.api_base_url()).rstrip("/")
    if not base.endswith("/v1"): base += "/v1"
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    payload = _json_object(network.get_sync(base + "/language-models", headers=headers, timeout=deadline.remaining(), proxy_purpose="oauth_xai", proxy_channel=proxy_channel))
    records = payload.get("data", payload.get("models"))
    if not isinstance(records, list): raise ValueError("xAI model catalog has invalid schema")
    records = [item for item in records if isinstance(item, dict)]
    models = _unique([item.get("id") or item.get("name") for item in records])
    if not models: raise ValueError("xAI model catalog has no verified language models")
    enrichment: dict[str, dict] = {}
    try:
        extra = _json_object(network.get_sync(base + "/models", headers=headers, timeout=deadline.remaining(), proxy_purpose="oauth_xai", proxy_channel=proxy_channel))
        values = extra.get("data", extra.get("models"))
        if isinstance(values, list): enrichment = {str(i.get("id") or i.get("name") or ""): i for i in values if isinstance(i, dict)}
    except Exception: pass
    normalized = []
    for item in records:
        model_id = str(item.get("id") or item.get("name") or "").strip(); merged = dict(item); supplement = enrichment.get(model_id) or {}
        if merged.get("context_length") is None and supplement.get("context_length") is not None: merged["context_length"] = supplement["context_length"]
        normalized.append(_record(model_id, merged, {
            "name": ("display_name", "name"), "description": ("description", "tagline"),
            "contextWindow": ("context_length", "context_window", "contextWindow"),
            "inputModalities": ("input_modalities", "inputModalities", "modalities"), "outputModalities": ("output_modalities", "outputModalities"),
            "reasoning": ("reasoning", "supports_reasoning"), "supportsImages": ("supports_images", "supportsImages"),
            "aliases": ("aliases",), "createdAt": ("created_at", "createdAt"),
        }))
    return DiscoveryResult(models, _catalog(normalized), "upstream:xai-language-models")


_ANTIGRAVITY_EXCLUDED_CATALOG_IDS = frozenset({
    # Antigravity IDE-internal/experimental entries.
    # They are not ordinary requestable account models.
    "chat_20706",
    "chat_23310",
    "tab_flash_lite_preview",
    "tab_jump_flash_lite_preview",
    "gemini-2.5-flash-thinking",
    "gemini-2.5-pro",
})


def _antigravity_is_text(record: dict, image_model_ids: set[str]) -> bool:
    """Accept upstream account models unless metadata positively says non-text.

    Antigravity's real catalog usually has no generic ``capabilities`` or
    ``modality`` field, so a positive allow-list would incorrectly couple live
    account discovery to the stateless configured fallback model list.
    """
    model_id = str(record.get("id") or "").strip()
    if (
        not model_id
        or model_id in image_model_ids
        or model_id in _ANTIGRAVITY_EXCLUDED_CATALOG_IDS
        or record.get("isInternal") is True
    ):
        return False

    kind = str(record.get("type") or record.get("modality") or "").lower()
    if kind in {"image", "image_generation", "video", "video_generation"}:
        return False

    caps = record.get("capabilities") or record.get("supportedGenerationMethods")
    text_caps = {"text", "chat", "completion", "responses", "generatecontent", "streamgeneratecontent"}
    non_text_caps = {"image", "image_generation", "video", "video_generation"}
    if isinstance(caps, dict):
        has_text = any(caps.get(k) is True for k in (
            "text", "chat", "completion", "responses", "generateContent", "streamGenerateContent",
        ))
        has_non_text = any(caps.get(k) is True for k in (
            "image", "image_generation", "video", "video_generation",
        ))
        if has_non_text and not has_text:
            return False
    elif isinstance(caps, list):
        normalized = {str(x).lower() for x in caps}
        if normalized & non_text_caps and not normalized & text_caps:
            return False
    return True


def discover_antigravity(account: dict, *, timeout: float = _TIMEOUT, proxy_channel: str = "") -> DiscoveryResult:
    deadline = _Deadline(timeout)
    token = str(account.get("access_token") or "")
    project_id = str(account.get("project_id") or account.get("projectId") or "")
    if not token or not project_id:
        raise ValueError("missing access token or project_id")
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": antigravity_provider.request_user_agent(),
        "x-goog-api-client": antigravity_provider.goog_api_client(),
    }
    body = {"project": project_id}
    image_model_ids = set(antigravity_provider.image_models())
    merged: dict[str, dict[str, Any]] = {}
    last_error: Exception | None = None
    for base in (antigravity_provider.api_base_url(), antigravity_provider.daily_api_base_url()):
        try:
            payload = _json_object(network.post_sync(
                base.rstrip("/") + "/v1internal:fetchAvailableModels",
                headers=headers, json=body, timeout=deadline.remaining(),
                proxy_purpose="oauth_antigravity", proxy_channel=proxy_channel,
            ))
            raw = payload.get("models")
            if not isinstance(raw, dict):
                raise ValueError("Antigravity model catalog has invalid models schema")
            records = []
            for model_id, value in raw.items():
                record = dict(value) if isinstance(value, dict) else {}
                record.setdefault("id", model_id)
                records.append(record)
            models = _unique([
                item["id"] for item in records
                if _antigravity_is_text(item, image_model_ids)
            ])
            if not models:
                raise ValueError("Antigravity catalog has no verified text models")
            wanted = set(models)
            for item in records:
                model_id = item["id"]
                if model_id not in wanted:
                    continue
                # Daily is queried second and refreshes duplicate metadata while
                # dict assignment preserves the model's first-seen position.
                merged[model_id] = _record(model_id, item, {
                    "name": ("displayName", "name"), "description": ("description",), "tagline": ("tag", "tagline"),
                    "contextWindow": ("maxTokens", "contextWindow"), "maxOutputTokens": ("maxOutputTokens",),
                    "reasoning": ("supportsThinking",), "supportsThinking": ("supportsThinking",), "supportsImages": ("supportsImages",),
                    "inputModalities": ("inputModalities",), "outputModalities": ("outputModalities",),
                })
        except Exception as exc:
            last_error = exc
    if merged:
        return DiscoveryResult(list(merged), _catalog(list(merged.values())), "upstream:antigravity")
    raise last_error or ValueError("Antigravity model discovery failed")


def discover_cursor(account: dict, *, timeout: float = _TIMEOUT, proxy_channel: str = "") -> DiscoveryResult:
    token = str(account.get("access_token") or "")
    if not token:
        raise ValueError("missing access token")
    if proxy_channel:
        payload = cursor_provider.fetch_model_catalog_sync(
            token, account_key=proxy_channel.removeprefix("oauth:"),
        )
    else:
        payload = cursor_provider.fetch_model_catalog_sync(token)
    records = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("Cursor model catalog has invalid schema")
    models = _unique([item.get("id") for item in records if isinstance(item, dict) and item.get("id")])
    if not models:
        raise ValueError("Cursor model catalog is empty")
    return DiscoveryResult(models, payload, "upstream:cursor")


ADAPTERS: dict[str, Callable[..., DiscoveryResult]] = {
    "openai": discover_openai,
    "claude": discover_claude,
    "xai": discover_xai,
    "antigravity": discover_antigravity,
    "cursor": discover_cursor,
}


def discover(account: dict, *, timeout: float = _TIMEOUT, proxy_channel: str = "") -> DiscoveryResult:
    from .oauth import normalize_provider
    provider = normalize_provider(account.get("provider") or account.get("type"))
    try:
        adapter = ADAPTERS[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported OAuth model provider: {provider}") from exc
    return adapter(account, timeout=timeout, proxy_channel=proxy_channel)
