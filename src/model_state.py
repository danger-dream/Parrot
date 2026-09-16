"""Persistent model-center state shared by management and runtime routing.

The configuration is authoritative.  This module deliberately keeps no cache so a
successful ``config.update`` is immediately visible to schedulers and discovery.
OAuth model disablement remains owned by each OAuth account; only global state and
API-source state live under ``modelCenter``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import config

SCHEMA_VERSION = 1


def _strings(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {
        item.strip() for item in value
        if isinstance(item, str) and item.strip()
    }


def state_snapshot(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root_cfg = config.get() if cfg is None else cfg
    raw = root_cfg.get("modelCenter") if isinstance(root_cfg, Mapping) else None
    root = raw if isinstance(raw, Mapping) else {}
    disabled = sorted(_strings(root.get("disabledModels")))
    hidden = sorted(_strings(root.get("hiddenModels")))
    api_raw = root.get("apiSourceDisabledModels")
    api_disabled: dict[str, list[str]] = {}
    if isinstance(api_raw, Mapping):
        for source_id, values in api_raw.items():
            source = str(source_id or "").strip()
            models = sorted(_strings(values))
            if source and models:
                api_disabled[source] = models
    return {
        "schemaVersion": SCHEMA_VERSION,
        "disabledModels": disabled,
        "hiddenModels": hidden,
        "apiSourceDisabledModels": dict(sorted(api_disabled.items())),
    }


def _root_for_write(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("modelCenter")
    if not isinstance(raw, dict):
        raw = {}
        cfg["modelCenter"] = raw
    raw["schemaVersion"] = SCHEMA_VERSION
    if not isinstance(raw.get("disabledModels"), list):
        raw["disabledModels"] = sorted(_strings(raw.get("disabledModels")))
    if not isinstance(raw.get("hiddenModels"), list):
        raw["hiddenModels"] = sorted(_strings(raw.get("hiddenModels")))
    if not isinstance(raw.get("apiSourceDisabledModels"), dict):
        raw["apiSourceDisabledModels"] = {}
    return raw


def is_global_enabled(model_id: str, cfg: Mapping[str, Any] | None = None) -> bool:
    model = str(model_id or "").strip()
    return bool(model) and model not in set(state_snapshot(cfg)["disabledModels"])


def is_discovery_visible(model_id: str, cfg: Mapping[str, Any] | None = None) -> bool:
    model = str(model_id or "").strip()
    snapshot = state_snapshot(cfg)
    return (
        bool(model)
        and model not in set(snapshot["disabledModels"])
        and model not in set(snapshot["hiddenModels"])
    )


def is_source_enabled(
    source_key: str,
    model_id: str,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """Read authoritative source preferences, even when channel reload failed."""
    source = str(source_key or "").strip()
    model = str(model_id or "").strip()
    if not source or not model:
        return True
    if source.startswith("api:"):
        values = state_snapshot(cfg)["apiSourceDisabledModels"].get(source, ())
        return model not in set(values)
    if source.startswith("oauth:"):
        from . import model_names
        from .oauth_ids import account_key
        root = config.get() if cfg is None else cfg
        for account in root.get("oauthAccounts") or ():
            if not isinstance(account, dict):
                continue
            try:
                if f"oauth:{account_key(account)}" != source:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            field = "cursor_disabled_models" if source.startswith("oauth:cursor:") else "disabledModels"
            return model_names.upstream_for_channel(source, model) not in _strings(account.get(field))
    return True


def set_global_enabled_in_config(
    cfg: dict[str, Any], model_ids: Iterable[str], enabled: bool,
) -> None:
    root = _root_for_write(cfg)
    values = _strings(root.get("disabledModels"))
    for model in _strings(model_ids):
        if enabled:
            values.discard(model)
        else:
            values.add(model)
    root["disabledModels"] = sorted(values)


def set_visible_in_config(
    cfg: dict[str, Any], model_ids: Iterable[str], visible: bool,
) -> None:
    root = _root_for_write(cfg)
    values = _strings(root.get("hiddenModels"))
    for model in _strings(model_ids):
        if visible:
            values.discard(model)
        else:
            values.add(model)
    root["hiddenModels"] = sorted(values)


def set_api_source_enabled_in_config(
    cfg: dict[str, Any], source_id: str, model_ids: Iterable[str], enabled: bool,
) -> None:
    source = str(source_id or "").strip()
    if not source.startswith("api:"):
        raise ValueError("API source id must start with 'api:'")
    root = _root_for_write(cfg)
    mapping = root["apiSourceDisabledModels"]
    values = _strings(mapping.get(source))
    for model in _strings(model_ids):
        if enabled:
            values.discard(model)
        else:
            values.add(model)
    if values:
        mapping[source] = sorted(values)
    else:
        mapping.pop(source, None)


def remove_api_source_in_config(cfg: dict[str, Any], source_id: str) -> None:
    root = _root_for_write(cfg)
    root["apiSourceDisabledModels"].pop(str(source_id or "").strip(), None)


def rename_api_source_in_config(
    cfg: dict[str, Any], old_source_id: str, new_source_id: str,
) -> None:
    root = _root_for_write(cfg)
    mapping = root["apiSourceDisabledModels"]
    old = str(old_source_id or "").strip()
    new = str(new_source_id or "").strip()
    if not old or not new or old == new or old not in mapping:
        return
    values = _strings(mapping.pop(old)) | _strings(mapping.get(new))
    if values:
        mapping[new] = sorted(values)


def known_model(model_id: str, cfg: Mapping[str, Any] | None = None) -> bool:
    """Check complete configured/LKG membership, including disabled models."""

    model = str(model_id or "").strip()
    if not model:
        return False
    root = config.get() if cfg is None else cfg
    for entry in root.get("channels") or ():
        if not isinstance(entry, Mapping):
            continue
        for item in entry.get("models") or ():
            if isinstance(item, Mapping):
                visible = item.get("alias") or item.get("real")
            else:
                visible = item
            if isinstance(visible, str) and visible.strip() == model:
                return True
    # Account ``models`` is the last-known-good route list. Disabled IDs are
    # retained separately but only count as known if they remain in that LKG.
    for account in root.get("oauthAccounts") or ():
        if not isinstance(account, Mapping):
            continue
        for value in account.get("models") or ():
            if isinstance(value, str) and value.strip() == model:
                return True
    return False
