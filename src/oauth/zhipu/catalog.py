"""Official account-endpoint model discovery; ZCode supplies metadata, not IDs.

A listed model is advertised by the upstream, not proof of subscription access.
"""
from __future__ import annotations

import copy
import re
import platform
import time
from urllib.parse import urlsplit, urlencode

from . import common as c

def model_record(model):
    value = model.lower()
    record = {"id": model, "name": model}
    if value in {"glm-5.3", "glm-5.3-flash"}:
        record.update(contextWindow=1000000, maxOutputTokens=128000,
                      reasoningEfforts=["low", "high", "max"], supportsImages=value.endswith("flash"))
    elif value == "glm-5.2":
        record.update(contextWindow=1000000, maxOutputTokens=128000, reasoningEfforts=["disabled", "high", "max"])
    elif value.startswith(("glm-5", "glm-4.")):
        record.update(reasoningEfforts=["disabled", "enabled"])
    return record


def _merge(target, patch):
    for key, value in patch.items():
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _matches(pattern, value):
    if not isinstance(pattern, str):
        return False
    try:
        return re.search(pattern, value, re.IGNORECASE) is not None
    except re.error:
        raise c.ZhipuError("catalog", "invalid_model_rule") from None


def configured_record(model, rules):
    """Resolve ordered model/API capability rules, never execute remote maps.

    This channel uses Anthropic messages for all ingresses. Capability data is
    descriptive; user metadata overrides and request values stay authoritative.
    Missing official values remain unknown rather than acquiring static guesses.
    """
    resolved = {}
    for group in ("modelRules", "modelApiRules"):
        for row in rules.get(group) or []:
            if not isinstance(row, dict) or not _matches(row.get("modelMatch"), model):
                continue
            if group == "modelApiRules" and not _matches(row.get("apiTypeMatch"), "anthropic-messages"):
                continue
            if isinstance(row.get("config"), dict):
                _merge(resolved, row["config"])
    properties = resolved.get("properties") or {}
    options = resolved.get("optionSpecs") or {}
    record = {"id": model, "name": model}
    for field, value in (("contextWindow", properties.get("contextWindow")),
                         ("maxOutputTokens", (options.get("maxOutputTokens") or {}).get("max"))):
        if type(value) in (int, float) and 0 < value < float("inf"):
            record[field] = value
    values = (options.get("reasoningLevel") or {}).get("values")
    if isinstance(values, list) and all(isinstance(value, str) for value in values):
        record["reasoningEfforts"] = list(dict.fromkeys(values))
    for field, source in (("toolCall", "supportsToolCall"), ("structuredOutput", "supportsJsonSchemaOutput")):
        if type(properties.get(source)) is bool:
            record[field] = properties[source]
    for source, target in (("inputFormat", "inputModalities"), ("outputFormat", "outputModalities")):
        formats = properties.get(source)
        if isinstance(formats, dict):
            record[target] = [name for name, key in (("text", "supportsText"), ("image", "supportsImage"),
                ("video", "supportsVideo"), ("audio", "supportsAudio"), ("pdf", "supportsPdf")) if formats.get(key) is True]
            if source == "inputFormat" and type(formats.get("supportsImage")) is bool:
                record["supportsImages"] = formats["supportsImage"]
    return record


def client_config_platform():
    # ZCode resolveRuntimePlatform (aZe), NOT the identity header's linux-x64.
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    return f"{os_name}-{arch}"


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise c.ZhipuError("catalog", "timeout")
    return remaining


def _fetch_model_rows(account, account_key, deadline):
    key = c.text(account.get("model_key"), "model_key", required=True)
    headers = {"Authorization": "Bearer " + key, "x-api-key": key,
               "anthropic-version": "2023-06-01"}
    if account.get("plan_scope") == "team":
        headers.update(c.scope_headers(account))
    endpoint = c.MODEL_ORIGINS[c.site_of(account)] + "/api/anthropic/v1/models"
    rows, seen, cursors = [], set(), set()
    cursor = ""
    for _ in range(10):
        url = endpoint + ("?" + urlencode({"after_id": cursor}) if cursor else "")
        data = c.request(url, headers=headers, account_key=account_key,
                         timeout=_remaining(deadline), envelope=False)
        if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("data"), list):
            raise c.ZhipuError("catalog", "invalid_data")
        page = data["data"]
        for row in page:
            model = row.get("id") if isinstance(row, dict) else None
            if not isinstance(model, str) or not 0 < len(model.strip()) <= 200:
                raise c.ZhipuError("catalog", "invalid_model_id")
            model = model.strip()
            if model.casefold() not in seen:
                seen.add(model.casefold())
                rows.append({**row, "id": model})
            if len(rows) > 500:
                raise c.ZhipuError("catalog", "too_large")
        more = data.get("has_more", data.get("hasMore", False))
        if type(more) is not bool:
            raise c.ZhipuError("catalog", "invalid_pagination")
        if not more:
            if not rows:
                raise c.ZhipuError("catalog", "empty_catalog")
            return rows
        cursor = data.get("last_id", data.get("lastId"))
        if not page or not isinstance(cursor, str) or not cursor or len(cursor) > 200 or cursor in cursors:
            raise c.ZhipuError("catalog", "invalid_pagination")
        cursors.add(cursor)
    raise c.ZhipuError("catalog", "too_many_pages")


def _fetch_capability_rules(account_key, deadline):
    data = c.request(c.PLATFORM_ORIGIN + "/api/v1/client/configs?" + urlencode({"app_version": c.VERSION, "platform": client_config_platform()}),
                     account_key=account_key, timeout=_remaining(deadline))
    if not isinstance(data, dict) or not isinstance(data.get("configs"), dict):
        raise c.ZhipuError("catalog", "invalid_data")
    url = data["configs"].get("builtin_provider_config_json")
    if not url:
        return None
    parsed = urlsplit(str(url))
    # This public metadata request never receives account credentials.
    import ipaddress
    host = parsed.hostname or ""
    try:
        is_ip = ipaddress.ip_address(host) is not None
    except ValueError:
        is_ip = False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or is_ip or "." not in host or host.endswith((".local", ".localhost")):
        raise c.ZhipuError("catalog", "invalid_url")
    directory = c.request(url, account_key=account_key, timeout=_remaining(deadline), envelope=False)
    try:
        capabilities = directory["config"]["modelConfigRules"]
        if not isinstance(capabilities, dict):
            raise ValueError()
        return capabilities
    except (KeyError, TypeError, ValueError):
        raise c.ZhipuError("catalog", "invalid_data") from None


def fetch_models(account, *, account_key="", timeout=20):
    deadline = time.monotonic() + timeout
    rows = _fetch_model_rows(account, account_key, deadline)
    try:
        capabilities = _fetch_capability_rules(account_key, deadline)
    except (c.ZhipuError, ValueError):
        # Optional metadata must not turn a valid model-list response into the
        # old two-model default. Reuse matching LKG metadata, not its membership.
        capabilities = None
    previous_ids = {str(model).casefold(): str(model) for model in account.get("models") or []}
    previous_records = {str(row.get("id") or "").casefold(): row
                        for row in (account.get("account_model_catalog") or {}).get("models") or []
                        if isinstance(row, dict)}
    records = []
    for row in rows:
        upstream_id = row["id"]
        # Keep existing client IDs/overrides/disable preferences intact when the
        # old ZCode directory used GLM-* but the endpoint uses glm-*.
        model = previous_ids.get(upstream_id.casefold(), upstream_id)
        if capabilities is None:
            record = copy.deepcopy(previous_records.get(upstream_id.casefold(), {}))
            record.update(id=model, name=model)
        else:
            record = configured_record(model, capabilities)
        name = row.get("display_name")
        if isinstance(name, str) and name.strip():
            record["name"] = name.strip()[:200]
        records.append(record)
    return records
