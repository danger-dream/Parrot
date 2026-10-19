"""Account catalogs from authenticated product config, without builtin fallbacks."""
from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from . import common


_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}


def _model_entries(models) -> dict[str, dict]:
    if not isinstance(models, list):
        raise common.WorkBuddyError("models", kind="invalid_catalog")
    entries = {}
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id = common.text(item["id"], "model", required=True)
        entries[model_id] = dict(item, id=model_id)
    return entries


def _allowed(source: dict, whitelist: list, *, ide: bool) -> bool:
    if not whitelist:
        return True
    model_id = source["id"]
    if model_id in whitelist or model_id.removeprefix("custom-local:") in whitelist:
        return True
    return ide and ("custom" in (source.get("tags") or []) or source.get("vendor") == "user"
                    or source.get("modelType") == "enterprise")


def _resolve(value, entries: dict[str, dict], *, aliases: bool) -> str | None:
    if not isinstance(value, str):
        return None
    if value in entries:
        return value
    if aliases:
        matches = [key for key, entry in entries.items() if value == entry.get("name") or (
            isinstance(entry.get("aliases"), list) and value in entry["aliases"])]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise common.WorkBuddyError("models", kind="ambiguous_alias")
    return None


def credit_multiplier(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    match = re.fullmatch(r"[xX×]?\s*(\d+(?:\.\d*)?|\.\d+)(?:\s+credits)?", text) if len(text) <= 64 else None
    if not match:
        return None
    try:
        number = Decimal(match[1])
    except InvalidOperation:
        return None
    return format(number, "f")


def _record(source: dict, *, agent: str, ide: bool, legacy: bool, gzip: bool | None) -> dict:
    model_id = source["id"]
    record = {"id": model_id, "name": common.text(source.get("name") or model_id, "model name"),
              "source": f"workbuddy:{agent}", "inputModalities": ["text"], "outputModalities": ["text"]}
    multiplier = credit_multiplier(source.get("credits"))
    if multiplier is not None:
        record["workbuddyCreditMultiplier"] = multiplier
    for key in ("maxInputTokens", "maxOutputTokens", "maxAllowedSize", "maxImageCount"):
        value = common.number(source.get(key))
        if isinstance(value, int) and value > 0:
            record[key] = value
    reasoning = source.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    efforts = reasoning.get("supportedEfforts")
    if isinstance(efforts, list):
        allowed = _EFFORTS | {"none"} if legacy else _EFFORTS
        record["reasoningEfforts"] = list(dict.fromkeys(item for item in efforts if isinstance(item, str) and item in allowed))
    if legacy:
        return record
    record["wireModel"] = common.text(source.get("modelId") or model_id, "wire model", required=True) if ide else model_id
    if source.get("supportsImages") is True and source.get("disabledMultimodal") is not True:
        record["inputModalities"].append("image")
    for key in ("supportsToolCall", "supportsReasoning", "onlyReasoning"):
        if isinstance(source.get(key), bool):
            record[key] = source[key]
    if isinstance(reasoning.get("canDisableThinking"), bool):
        record["canDisableThinking"] = reasoning["canDisableThinking"]
    if source.get("onlyReasoning") is True:
        record["canDisableThinking"] = False
    default = reasoning.get("effort") or reasoning.get("defaultEffort")
    if isinstance(default, str) and default in _EFFORTS:
        record["defaultReasoningEffort"] = default
    if isinstance(reasoning.get("summary"), str):
        record["reasoningSummary"] = common.text(reasoning["summary"], "reasoning summary")
    if gzip is not None:
        record["requestBodyGzip"] = gzip
    return record


def fetch_models_sync(account: dict, *, account_key: str = "", timeout: float = 20.0) -> list[dict]:
    legacy = common.is_legacy(account)
    ide = common.profile_of(account) == common.GLOBAL_PROFILE
    deadline = time.monotonic() + max(0.001, timeout)
    if legacy:
        try:
            data = common.request(account, "/console/enterprises/personal/models", method="GET",
                                  kind="account", account_key=account_key, timeout=timeout)
        except common.WorkBuddyError as exc:
            if exc.status_code not in {404, 405}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WorkBuddy model discovery deadline exhausted") from None
            data = common.request(account, "/v3/config", method="GET",
                                  kind="account", account_key=account_key, timeout=remaining)
    else:
        data = common.request(account, "/v3/config", method="GET",
                              kind="models", account_key=account_key, timeout=timeout)
    entries = _model_entries(data.get("models"))
    agents = data.get("agents")
    if not isinstance(agents, list):
        raise common.WorkBuddyError("models", kind="invalid_catalog")
    agent = "craft" if ide else "cli"
    client_models = next((item.get("models") for item in agents if isinstance(item, dict) and item.get("name") == agent), None)
    if not isinstance(client_models, list):
        raise common.WorkBuddyError("models", kind=f"missing_{agent}_catalog")
    client_models = list(client_models)
    whitelist = data.get("availableModels") if not legacy else None
    if whitelist is not None and (not isinstance(whitelist, list) or any(not isinstance(item, str) for item in whitelist)):
        raise common.WorkBuddyError("models", kind="invalid_catalog")
    whitelist = whitelist or []
    if not ide:
        entries = {key: item for key, item in entries.items() if _allowed(item, whitelist, ide=False)}
    enterprise = account.get("enterprise_id") or (data.get("enterpriseId") if not legacy else "")
    if enterprise and not legacy:
        enterprise = common.text(enterprise, "enterprise_id", required=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("WorkBuddy model discovery deadline exhausted")
        enterprise_models = common.request(dict(account, enterprise_id=enterprise),
            f"/console/enterprises/{quote(enterprise, safe='')}/config/models", method="GET", kind="models",
            account_key=account_key, timeout=remaining, allow_list=True)
        if not isinstance(enterprise_models, list):
            raise common.WorkBuddyError("models", kind="invalid_catalog")
        enterprise_entries = _model_entries(enterprise_models)
        for key, item in enterprise_entries.items():
            entries[key] = {**entries.get(key, {}), **item,
                            "modelType": "enterprise" if key.startswith("custom:") else "built-in"}
        chat_models = [key for key, item in enterprise_entries.items() if "chat" in (item.get("tags") or [])]
        if ide:
            client_models.extend(chat_models)
            enterprise_whitelist = [key for key in enterprise_entries if not key.startswith("custom:")]
            if enterprise_whitelist:
                whitelist = enterprise_whitelist
        else:
            client_models = chat_models + client_models
    if ide:
        entries = {key: item for key, item in entries.items() if _allowed(item, whitelist, ide=True)}
    elif not legacy:
        client_models.extend(key for key, item in entries.items() if key.startswith("custom:") or
                             any(tag in (item.get("tags") or []) for tag in ("custom", "chat")))
    features = data.get("productFeatures")
    # Both pinned modern profiles enable gzip; cloud configuration may disable it.
    gzip = features.get("RequestBodyGzip", True) if isinstance(features, dict) else True
    gzip = gzip if isinstance(gzip, bool) and not legacy else None
    records, seen = [], set()
    for value in client_models:
        model_id = _resolve(value, entries, aliases=not legacy and not ide)
        if model_id is None or model_id in seen:
            continue
        source = entries[model_id]
        if source.get("disabled") is True:
            continue
        # Local/custom direct-connect credentials are not WorkBuddy OAuth routes.
        if not legacy and (source.get("vendor") == "user" or source.get("local") is True or source.get("url") or source.get("apiKey")):
            continue
        seen.add(model_id)
        records.append(_record(source, agent=agent, ide=ide, legacy=legacy, gzip=gzip))
    if not records:
        raise common.WorkBuddyError("models", kind="empty_catalog")
    return records
