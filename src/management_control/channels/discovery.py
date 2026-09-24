"""Transport-neutral channel model discovery orchestration and injectable seam."""

from __future__ import annotations

from typing import Any

from src.management_auth.principal import Capability
from src.models_discovery import (
    ModelsDiscoveryError,
    derive_custom_models_url,
    discover_models as _discover_models,
)
from src.providers.catalog import get_preset

from ..errors import ManagementError, ManagementErrorCode
from .models import DiscoveryCommand, DiscoveryResult
from .validation import validate_base_url


async def discover_models(endpoint: str, api_key: str, **kwargs: Any) -> list[str]:
    """Stable injectable seam used by both ChannelControl and the TG harness."""
    return await _discover_models(endpoint, api_key, **kwargs)


async def run_model_discovery(
    control: Any,
    context: Any,
    command: DiscoveryCommand,
    *,
    discoverer=discover_models,
) -> DiscoveryResult:
    """Resolve existing/draft credentials and discover without persisting models."""
    control._authorize(context, Capability.WRITE)
    preset = command if command.catalog_override else None
    key = command.api_key
    base_url = command.base_url
    api_path = command.api_path
    provider = command.provider_id or ""
    if command.channel_id:
        channel = control._domain_channel(command.channel_id)
        key = str(getattr(channel, "api_key", "") or "")
        base_url = channel.base_url
        api_path = getattr(channel, "api_path", None)
        provider = getattr(channel, "provider_id", "") or ""
        if preset is None:
            preset = get_preset(
                getattr(channel, "provider_id", "") or "",
                getattr(channel, "provider_preset_id", "") or "",
            )
    elif (command.provider_id or command.provider_preset_id) and preset is None:
        preset = get_preset(command.provider_id or "", command.provider_preset_id or "")
        if preset is None:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
    if not command.channel_id:
        control._authorize(context, Capability.SECRETS_WRITE)
    if not key:
        raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)

    error = None
    source = "live"
    models: list[str] = []
    try:
        if preset and preset.models_url:
            models = await discoverer(
                preset.models_url,
                key,
                auth=preset.models_auth,
                parser=preset.models_parser,
                provider=provider, channel_key=command.channel_id or "",
            )
        elif preset and preset.static_models:
            models = list(preset.static_models)
            source = "static"
        elif preset:
            raise ModelsDiscoveryError("该提供商未公开模型列表")
        elif base_url:
            validate_base_url(base_url)
            models = await discoverer(
                derive_custom_models_url(base_url, api_path), key,
                provider=provider, channel_key=command.channel_id or "",
            )
        else:
            raise ModelsDiscoveryError("无法从 URL 推导模型列表地址")
    except ModelsDiscoveryError as exc:
        error = str(exc)
        if preset and preset.static_models:
            models = list(preset.static_models)
            source = "static"
    retry = bool((preset and preset.models_url) or not preset)
    return DiscoveryResult(tuple(models), source, error, retry)
