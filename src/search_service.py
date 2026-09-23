"""Search backends shared by tool execution and management.

No conversation-route mutation, and no credentials in public results.
A backend attempt includes OAuth acquisition, network I/O and response parsing
under one deadline. Search retries are bounded across all keys and backends.
"""
from __future__ import annotations

import asyncio
import copy
import html
import ipaddress
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import config, network
from .search_xai import (
    X_SEARCH_ONLY_FIELDS,
    build_evidence_index as _xai_build_evidence_index,
    evidence_key as _xai_evidence_key,
    is_x_search_call_item,
    normalize_x_search_date,
)

MODES = ("managed", "passthrough", "disabled")
BACKEND_TYPES = ("anysearch", "tavily", "exa", "brave", "openai", "xai", "anthropic", "zhipu")
KEY_TYPES = frozenset(("anysearch", "tavily", "exa", "brave", "zhipu"))
ACCOUNT_TYPES = frozenset(("openai", "xai", "anthropic", "zhipu"))
EXTRACT_TYPES = frozenset(("anysearch", "tavily", "exa", "openai", "zhipu"))
ENDPOINTS = {
    "anysearch": "https://api.anysearch.com",
    "tavily": "https://api.tavily.com",
    "exa": "https://api.exa.ai",
    "brave": "https://api.search.brave.com",
}
NAMES = {"anysearch": "AnySearch", "tavily": "Tavily", "exa": "Exa", "brave": "Brave",
         "openai": "OpenAI OAuth", "xai": "xAI OAuth", "anthropic": "Anthropic OAuth", "zhipu": "智谱 MCP"}
DEFAULTS = {
    "functionMode": "managed", "hostedMode": "managed", "maxAttempts": 3,
    "timeoutSeconds": 10, "maxResults": 8, "maxToolRounds": 50,
    "maxFetchChars": 50000, "minQueryChars": 2, "maxFetchUrlChars": 2048,
    "requireKnownUrlForFetch": True, "maxConcurrentToolCalls": 0,
    "language": "", "country": "", "freshness": "",
}
_FRESH_DAYS = {"day": 1, "week": 7, "month": 31, "year": 365}
# Portable returned-text budgets, not guesses at a provider's tokenizer/window.
_CONTEXT_CHARS = {"low": 4000, "medium": 12000, "high": 24000}


class SearchError(RuntimeError):
    def __init__(self, message: str, *, code: str = "search_failed", status_code: int = 502,
                 retryable: bool = True):
        super().__init__(message)
        self.message, self.code, self.status_code, self.retryable = message, code, status_code, retryable


def default_backend(kind: str) -> dict:
    return {"id": kind, "type": kind, "name": NAMES[kind], "enabled": True,
            "apiKeys": [], "endpoint": ENDPOINTS.get(kind, ""), "model": "",
            "accountIds": [], "allowDisabledAccounts": False}


def settings() -> dict:
    """Effective config; intentionally private (it contains secrets). Never persist on read."""
    cfg = config.get()
    legacy = cfg.get("anysearch") or {}
    current = cfg.get("search") or {}
    result = copy.deepcopy(DEFAULTS)
    if isinstance(legacy, dict):
        for key in ("timeoutSeconds", "maxResults", "maxFetchChars", "maxToolRounds", "minQueryChars",
                    "maxFetchUrlChars", "requireKnownUrlForFetch", "maxConcurrentToolCalls"):
            if key in legacy:
                result[key] = copy.deepcopy(legacy[key])
        # Explicit old opt-out must not silently start interception on upgrade.
        if legacy.get("enabled") is False:
            result.update(functionMode="passthrough", hostedMode="passthrough")
    if isinstance(current, dict):
        result.update(copy.deepcopy(current))
    if "backends" not in result:
        result["backends"] = [default_backend(kind) for kind in BACKEND_TYPES]
        key = str(legacy.get("apiKey") or os.environ.get("ANYSEARCH_API_KEY", "")).strip()
        result["backends"][0]["apiKeys"] = [key] if key else []
        endpoint = str(legacy.get("endpoint") or ENDPOINTS["anysearch"]).rstrip("/")
        # Known legacy MCP URL migrates to the same origin's documented REST API.
        result["backends"][0]["endpoint"] = endpoint.removesuffix("/mcp")
    return result


def _keys(backend: dict) -> list[str]:
    values = backend.get("apiKeys") or []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(x).strip() for x in values if str(x).strip()))


def _accounts(backend: dict) -> list[dict]:
    from .oauth_ids import account_key
    provider = "claude" if backend["type"] == "anthropic" else backend["type"]
    selected = {str(x).removeprefix("oauth:") for x in backend.get("accountIds") or []}
    result = []
    for account in config.get().get("oauthAccounts") or []:
        actual = account.get("provider") or "claude"
        if actual != provider or not account.get("model_key" if provider == "zhipu" else "access_token"):
            continue
        if provider == "zhipu" and account.get("site") not in ("bigmodel", "zai"):
            continue
        if selected and account_key(account) not in selected:
            continue
        reason = account.get("disabled_reason")
        if reason not in (None, "", "user"):
            continue  # auth/quota/system failures are not a manual chat-only opt-out
        if not backend.get("allowDisabledAccounts", False) and (
            account.get("enabled", True) is False or reason
        ):
            continue
        # A deleted/re-created account is not matched by display name or order.
        result.append(account)
    return result


def _credentials(backend: dict) -> list:
    kind = backend["type"]
    keys = _keys(backend) if kind in KEY_TYPES else []
    accounts = _accounts(backend) if kind in ACCOUNT_TYPES else []
    if kind == "zhipu":
        # Do not try the same Key twice when it is both explicitly configured
        # and referenced by an account on the same site.
        from .oauth.zhipu.common import MODEL_ORIGINS
        origin = str(backend.get("endpoint") or MODEL_ORIGINS["bigmodel"]).rstrip("/")
        seen = {(origin, key) for key in keys}
        unique = []
        for account in accounts:
            identity = (MODEL_ORIGINS[account["site"]], account["model_key"])
            if identity not in seen:
                seen.add(identity)
                unique.append(account)
        accounts = unique
    return keys + accounts


def backend_statuses() -> list[dict]:
    """Configuration readiness, not a synthetic live probe. No credential-bearing fields."""
    rows = []
    for backend in settings().get("backends") or []:
        kind = backend.get("type")
        if kind not in BACKEND_TYPES:
            continue
        key_count = len(_keys(backend)) if kind in KEY_TYPES else 0
        account_count = len(_accounts(backend)) if kind in ACCOUNT_TYPES else 0
        count = key_count + account_count
        enabled = backend.get("enabled", True) is not False
        rows.append({"id": backend["id"], "type": kind, "name": backend.get("name") or NAMES[kind],
                     "enabled": enabled, "available": enabled and count > 0,
                     "reason": "disabled" if not enabled else ("configured" if count else
                                ("missing_credentials" if kind in KEY_TYPES else "no_eligible_accounts")),
                     "keyCount": key_count,
                     "accountCount": account_count,
                     "verified": kind != "anthropic"})
    return rows


def _domains(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SearchError("域名限制必须为数组", code="invalid_search_input", status_code=400, retryable=False)
    result = []
    for raw in value:
        value = str(raw).strip().lower().removeprefix("*.")
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = parsed.hostname or ""
        if not host or re.search(r"[\s()\"']", value) or parsed.username or parsed.password:
            raise SearchError("域名限制含无效域名", code="invalid_search_input", status_code=400, retryable=False)
        result.append(host.encode("idna").decode("ascii"))
    return list(dict.fromkeys(result))


def _domain_matches(url: str, domains: list[str]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _check_extract_domains(url: str, args: dict) -> None:
    if ((args["allowed_domains"] and not _domain_matches(url, args["allowed_domains"]))
            or (args["blocked_domains"] and _domain_matches(url, args["blocked_domains"]))):
        raise SearchError("提取URL不符合工具的域名限制", code="url_not_allowed", status_code=400, retryable=False)


def _query_text(args: dict) -> str:
    """Ranking preferences also survive providers without dedicated locale fields."""
    query = args["query"]
    preferences = []
    if args.get("language"):
        preferences.append("preferred language: " + str(args["language"]))
    if args.get("country"):
        preferences.append("region: " + str(args["country"]))
    location = args.get("user_location") or {}
    if location:
        preferences.append("approximate user location: " + ", ".join(
            f"{field}={location[field]}" for field in ("city", "region", "country", "timezone") if location.get(field)
        ))
    if preferences:
        query += " (" + "; ".join(preferences) + ")"
    return query


def _query(args: dict) -> str:
    parts = []
    if args["allowed_domains"]:
        parts.append("(" + " OR ".join("site:" + d for d in args["allowed_domains"]) + ")")
    parts.append(_query_text(args))
    parts += ["-site:" + d for d in args["blocked_domains"]]
    return " ".join(parts)


def _x_handles(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise SearchError(f"{field}须为最多20个X用户名的数组", code="invalid_search_input",
                          status_code=400, retryable=False)
    output, seen = [], set()
    for raw in value:
        if not isinstance(raw, str):
            raise SearchError(f"{field}须为最多20个X用户名的数组", code="invalid_search_input",
                              status_code=400, retryable=False)
        handle = raw.strip().removeprefix("@").strip()
        if not handle or len(handle) > 64 or re.search(r"\s", handle):
            raise SearchError(f"{field}含无效X用户名", code="invalid_search_input",
                              status_code=400, retryable=False)
        folded = handle.casefold()
        if folded not in seen:
            seen.add(folded)
            output.append(handle)
    return output


def _x_date(value: Any, field: str):
    try:
        return normalize_x_search_date(value, field)
    except ValueError:
        raise SearchError(f"{field}须为ISO8601日期（YYYY-MM-DD）", code="invalid_search_input",
                          status_code=400, retryable=False) from None


def _normalize_x_search_arguments(args: dict) -> None:
    if args["allowed_domains"] or args["blocked_domains"]:
        raise SearchError(
            "原生X搜索不支持allowed_domains或blocked_domains；请使用allowed_x_handles或excluded_x_handles",
            code="unsupported_search_parameter", status_code=400, retryable=False,
        )
    if "external_web_access" in args and args["external_web_access"] not in (True, "live"):
        raise SearchError("原生X搜索只支持实时搜索", code="unsupported_search_parameter",
                          status_code=400, retryable=False)
    args["allowed_x_handles"] = _x_handles(args.get("allowed_x_handles"), "allowed_x_handles")
    args["excluded_x_handles"] = _x_handles(args.get("excluded_x_handles"), "excluded_x_handles")
    if args["allowed_x_handles"] and args["excluded_x_handles"]:
        raise SearchError("allowed_x_handles与excluded_x_handles不能同时使用",
                          code="invalid_search_input", status_code=400, retryable=False)
    from_date, parsed_from = _x_date(args.get("from_date"), "from_date")
    to_date, parsed_to = _x_date(args.get("to_date"), "to_date")
    if args.get("freshness") and (from_date or to_date):
        raise SearchError("freshness不能与from_date或to_date同时使用",
                          code="invalid_search_input", status_code=400, retryable=False)
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise SearchError("from_date不能晚于to_date", code="invalid_search_input",
                          status_code=400, retryable=False)
    args["from_date"], args["to_date"] = from_date, to_date
    for field in ("enable_image_understanding", "enable_video_understanding"):
        if field in args and not isinstance(args[field], bool):
            raise SearchError(f"{field}须为布尔值", code="invalid_search_input",
                              status_code=400, retryable=False)


def _arguments(arguments: dict, cfg: dict, operation: str) -> dict:
    args = copy.deepcopy(arguments)
    context_size = args.get("search_context_size")
    if context_size is not None and (not isinstance(context_size, str) or context_size not in _CONTEXT_CHARS):
        raise SearchError("search_context_size须为low/medium/high", code="invalid_search_input", status_code=400, retryable=False)
    location = args.get("user_location")
    if location is not None:
        if (not isinstance(location, dict) or set(location) - {"type", "city", "region", "country", "timezone"}
                or location.get("type", "approximate") != "approximate"
                or any(not isinstance(value, str) or len(value) > 256 for value in location.values())):
            raise SearchError("user_location须为合法的approximate位置对象", code="invalid_search_input", status_code=400, retryable=False)
    if "external_web_access" in args and args["external_web_access"] not in (True, False, "cached", "indexed", "live"):
        raise SearchError("external_web_access参数无效", code="invalid_search_input", status_code=400, retryable=False)
    for key in ("language", "country", "freshness"):
        if (key == "freshness" and operation == "x_search" and not args.get(key)
                and (args.get("from_date") or args.get("to_date"))):
            continue  # Exact X date bounds override a configured default freshness.
        if not args.get(key) and cfg.get(key):
            args[key] = cfg[key]
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    args["allowed_domains"] = _domains(args.get("allowed_domains", filters.get("allowed_domains")))
    args["blocked_domains"] = list(dict.fromkeys(
        domain for source in (args, filters) for field in ("blocked_domains", "excluded_domains")
        for domain in _domains(source.get(field))
    ))
    if operation == "extract":
        url = str(args.get("url") or "").strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise SearchError("提取URL格式无效", code="invalid_search_input", status_code=400, retryable=False) from None
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            address = None
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
                or parsed.hostname.lower() in ("localhost", "localhost.localdomain")
                or (address is not None and not address.is_global)):
            raise SearchError("提取URL必须是公开HTTP(S)地址", code="invalid_search_input", status_code=400, retryable=False)
        if len(url) > int(cfg["maxFetchUrlChars"]):
            raise SearchError("提取URL超过长度限制", code="invalid_search_input", status_code=400, retryable=False)
        _check_extract_domains(url, args)
        args["url"] = url
    else:
        args["query"] = str(args.get("query") or args.get("q") or "").strip()
        if len(args["query"]) < int(cfg["minQueryChars"]):
            raise SearchError("搜索词为空或过短", code="invalid_search_input", status_code=400, retryable=False)
        try:
            args["max_results"] = max(1, min(20, int(args.get("max_results", cfg["maxResults"]))))
        except (TypeError, ValueError):
            raise SearchError("搜索结果数量必须为整数", code="invalid_search_input", status_code=400, retryable=False)
        if args.get("freshness") and args["freshness"] not in _FRESH_DAYS:
            raise SearchError("时间范围须为day/week/month/year", code="invalid_search_input", status_code=400, retryable=False)
        if operation == "x_search":
            _normalize_x_search_arguments(args)
        else:
            unsupported = next((field for field in X_SEARCH_ONLY_FIELDS if field in args), None)
            if unsupported:
                raise SearchError(f"{unsupported}仅支持原生X搜索来源",
                                  code="unsupported_search_parameter", status_code=400, retryable=False)
    return args


def _endpoint(backend: dict, path: str) -> str:
    base = str(backend.get("endpoint") or ENDPOINTS[backend["type"]]).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SearchError("搜索后端地址配置无效", code="invalid_search_backend", status_code=400, retryable=False)
    if base.endswith(path):
        return base
    if base.endswith("/v1") and path.startswith("/v1/"):
        return base + path[3:]
    return base + path


def _check_response(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        status = response.status_code
        raise SearchError(f"搜索上游返回 HTTP {status}", code="search_upstream_error", status_code=502,
                          retryable=status in (408, 409, 425, 429) or status >= 500)
    try:
        data = response.json()
    except ValueError:
        raise SearchError("搜索上游返回非JSON内容", code="invalid_search_response") from None
    if not isinstance(data, dict):
        raise SearchError("搜索上游响应结构无效", code="invalid_search_response")
    if data.get("error"):
        raise SearchError("搜索上游报告执行错误", code="search_upstream_error")
    return data


def _normalize_rows(rows: list, args: dict, *, omit_missing_snippet: bool = False) -> list[dict]:
    output, seen = [], set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if urlsplit(url).scheme not in ("http", "https") or url in seen:
            continue
        if args["allowed_domains"] and not _domain_matches(url, args["allowed_domains"]):
            continue
        if args["blocked_domains"] and _domain_matches(url, args["blocked_domains"]):
            continue
        seen.add(url)
        text = str(item.get("snippet") or item.get("description") or item.get("content") or item.get("text") or "")
        row = {"title": str(item.get("title") or ""), "url": url}
        if text or not omit_missing_snippet:
            row["snippet"] = html.unescape(re.sub(r"</?(?:strong|b|em|mark)>", "", text))
        published = item.get("publishedDate") or item.get("published_at") or item.get("page_age")
        if published:
            row["published_at"] = published
        output.append(row)
    return output[:args["max_results"]]


def _xai_structured_output(text: str) -> tuple[list, str | None] | None:
    """Parse the result envelope requested from xAI without guessing from prose."""
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    answer = payload.get("answer")
    return payload["results"], answer if isinstance(answer, str) else None


def _xai_aligned_rows(structured: list, evidence: list, *, x_search: bool = False) -> list:
    """Keep structured rows tied to URLs observed in native search evidence."""
    evidence_by_url, x_user_citations = _xai_build_evidence_index(evidence, x_search=x_search)
    if not evidence_by_url:
        return structured
    aligned = []
    for item in structured:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        key = _xai_evidence_key(url, x_search=x_search)
        source = evidence_by_url.get(key)
        # Current xAI OAuth user-search citations expose only /i/user/<numeric-id>,
        # while the structured native result exposes the corresponding /<handle>.
        # The citation therefore proves one profile result but cannot be URL-keyed.
        if source is None and key.startswith("x-profile:") and x_user_citations:
            x_user_citations -= 1
            aligned.append(item)
            continue
        if source is None:
            continue
        merged = dict(source)
        for key, value in item.items():
            # Preserve source metadata when the structured envelope truthfully
            # omits (or leaves blank) a field that the native source supplied.
            if key in ("title", "snippet", "description", "content", "text") and not value:
                continue
            merged[key] = value
        aligned.append(merged)
    return aligned


async def _http_adapter(backend: dict, credential: str, args: dict, operation: str, cfg: dict) -> dict:
    kind = backend["type"]
    headers, params, payload = {"accept": "application/json"}, None, None
    method = "POST"
    freshness = args.get("freshness")
    if kind == "anysearch":
        headers["Authorization"] = "Bearer " + credential
        url = _endpoint(backend, "/v1/" + ("search" if operation == "search" else "extract"))
        payload = {"url": args["url"]} if operation == "extract" else {
            "query": _query(args), "max_results": min(10, args["max_results"])}
        if operation == "search":
            for key in ("language", "zone", "tag"):
                if args.get(key): payload[key] = args[key]
            if freshness: payload["params"] = {"freshness": freshness}
    elif kind == "tavily":
        headers["Authorization"] = "Bearer " + credential
        url = _endpoint(backend, "/search" if operation == "search" else "/extract")
        payload = {"urls": [args["url"]], "format": "markdown"} if operation == "extract" else {
            "query": _query_text(args), "max_results": args["max_results"], "search_depth": "basic",
            "include_domains": args["allowed_domains"], "exclude_domains": args["blocked_domains"]}
        if operation == "search" and freshness: payload["time_range"] = freshness
    elif kind == "exa":
        headers["x-api-key"] = credential
        url = _endpoint(backend, "/search" if operation == "search" else "/contents")
        contents = {"text": {"maxCharacters": int(cfg["maxFetchChars"]) if operation == "extract" else 2000}}
        payload = {"ids": [args["url"]], **contents} if operation == "extract" else {
            "query": _query_text(args), "numResults": args["max_results"], "type": "auto", "contents": contents}
        if operation == "search":
            if args["allowed_domains"]: payload["includeDomains"] = args["allowed_domains"]
            if args["blocked_domains"]: payload["excludeDomains"] = args["blocked_domains"]
            if freshness:
                payload["startPublishedDate"] = (datetime.now(timezone.utc) - timedelta(days=_FRESH_DAYS[freshness])).isoformat()
    else:  # Brave has search, not a general page extractor.
        if operation == "extract":
            raise SearchError("Brave不提供网页提取接口", code="search_capability_unavailable", retryable=False)
        headers["X-Subscription-Token"] = credential
        url, method = _endpoint(backend, "/res/v1/web/search"), "GET"
        params = {"q": _query(args), "count": args["max_results"], "extra_snippets": "true"}
        if freshness: params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[freshness]
        if args.get("country"): params["country"] = args["country"]
        if args.get("language"): params["search_lang"] = args["language"]
    async with network.async_client(timeout=httpx.Timeout(float(cfg["timeoutSeconds"])), follow_redirects=False) as client:
        response = await client.request(method, url, headers=headers, json=payload, params=params)
    data = _check_response(response)
    if kind == "anysearch":
        if data.get("code") != 0:
            raise SearchError("AnySearch报告搜索执行失败", code="search_upstream_error")
        data = data.get("data") or {}
        if not isinstance(data, dict):
            raise SearchError("AnySearch结果结构无效", code="invalid_search_response")
    try:
        return _parse_http_result(data, kind, args, operation, cfg)
    except Exception as exc:
        if kind != "exa":
            raise
        error = exc if isinstance(exc, SearchError) else SearchError(
            "搜索上游认证或响应处理失败", code="search_backend_error", retryable=False)
        raise _billing_failure(error, backend, data) from None


def _parse_http_result(data: dict, kind: str, args: dict, operation: str, cfg: dict) -> dict:
    # Only Exa supplies the explicit dollar total consumed by search settlement.
    # Keep the upstream body private, including when parsing paid results fails.
    result = {"_billing_body": data} if kind == "exa" else {}
    usage = data.get("usage") or data.get("costDollars")
    if usage is not None: result["usage"] = usage
    if operation == "extract":
        row = data if kind == "anysearch" else next(iter(data.get("results") or []), {})
        effective_url = str(row.get("url") or args["url"])
        _check_extract_domains(effective_url, args)
        content = row.get("content") or row.get("raw_content") or row.get("text")
        if not content:
            raise SearchError("上游没有返回网页正文", code="empty_extract_response")
        _set_extract_content(result, effective_url, str(content), cfg)
    else:
        rows = (data.get("web") or {}).get("results") if kind == "brave" else data.get("results")
        if not isinstance(rows, list):
            raise SearchError("搜索上游缺少结果数组", code="invalid_search_response")
        result.update(query=args["query"], results=_normalize_rows(rows, args))
        if data.get("answer"): result["answer"] = data["answer"]
    return result


def _set_extract_content(result: dict, url: str, content: str, cfg: dict) -> None:
    maximum = int(cfg["maxFetchChars"])
    result.update(url=url, content=content[:maximum])
    if len(content) > maximum:
        result["truncated"] = True
        result.setdefault("warnings", []).append(
            f"Content truncated to {maximum} characters by Parrot (maxFetchChars).")


def _billing_failure(error: SearchError, backend: dict, data: Any) -> SearchError:
    """Carry observed billing privately until this attempt is settled.

    Search semantics may fail after a paid upstream response. These fields are
    never part of the public error message or the eventual retry summary.
    """
    if isinstance(data, dict):
        error._billing_body = data
        error._billing_model = _effective_model(backend, {"_upstream_model": data.get("model")})
        error._billing_provider = backend["type"]
    return error


async def _oauth_adapter(backend: dict, account: dict, args: dict, operation: str, cfg: dict) -> dict:
    from . import oauth_manager
    from .oauth_ids import account_key
    from .openai.codex_constants import apply_codex_workspace_routing, codex_backend_base_url, codex_cli_user_agent, codex_cli_version, codex_originator
    kind = backend["type"]
    key = account_key(account)
    state_key = oauth_manager.account_state_key(account)
    headers = {"content-type": "application/json", "accept": "application/json"}
    if kind not in ("anthropic", "xai"):
        token = await oauth_manager.ensure_valid_token(key, expected_state_key=state_key)
        headers["authorization"] = "Bearer " + token
    model = str(backend.get("model") or {"openai": "gpt-5.5", "xai": "grok-4.6", "anthropic": "claude-sonnet-4-6"}[kind])
    if kind == "openai":
        with oauth_manager.account_generation_guard(state_key) as current:
            if not current:
                raise SearchError("搜索账户身份已变更", code="search_account_retired", status_code=503, retryable=False)
            account = copy.deepcopy(oauth_manager.get_account(key) or account)
        prov = config.get().get("openaiOAuth") or {}
        headers.update({"chatgpt-account-id": account.get("workspace_id") or account.get("chatgpt_account_id") or "",
                        "originator": codex_originator(prov), "user-agent": codex_cli_user_agent(prov), "version": codex_cli_version(prov)})
        search_settings = {"search_context_size": args.get("search_context_size") or "low"}
        filters = {key: args[key] for key in ("allowed_domains", "blocked_domains") if args[key]}
        if filters:
            search_settings["filters"] = filters
        if args.get("user_location"): search_settings["user_location"] = args["user_location"]
        if "external_web_access" in args: search_settings["external_web_access"] = args["external_web_access"]
        if operation == "extract":
            commands = {"open": [{"ref_id": args["url"]}], "response_length": "long"}
        else:
            query = {"q": _query_text(args)}
            if args.get("freshness"): query["recency"] = _FRESH_DAYS[args["freshness"]]
            if args["allowed_domains"]: query["domains"] = args["allowed_domains"]
            commands = {"search_query": [query], "response_length": "short"}
        payload = {"id": "parrot-search-" + uuid.uuid4().hex, "model": model, "commands": commands,
                   "settings": search_settings, "max_output_tokens": 2000}
        url, headers = apply_codex_workspace_routing((str(backend.get("endpoint") or codex_backend_base_url(prov))).rstrip("/") + "/alpha/search", headers, account)
    elif kind == "xai":
        if operation == "extract":
            raise SearchError("xAI搜索不作为网页正文提取接口", code="search_capability_unavailable", retryable=False)
        is_x_search = operation == "x_search"
        tool = {"type": "x_search" if is_x_search else "web_search"}
        if is_x_search:
            for field in ("allowed_x_handles", "excluded_x_handles", "from_date", "to_date",
                          "enable_image_understanding", "enable_video_understanding"):
                value = args.get(field)
                if value not in (None, [], False):
                    tool[field] = value
            if args.get("freshness"):
                tool["from_date"] = (
                    datetime.now(timezone.utc) - timedelta(days=_FRESH_DAYS[args["freshness"]])
                ).date().isoformat()
            query = _query_text(args)
            search_label, prompt_label = "X search", "Use X search to find"
        else:
            filters = {}
            if args["allowed_domains"]: filters["allowed_domains"] = args["allowed_domains"]
            if args["blocked_domains"]: filters["excluded_domains"] = args["blocked_domains"]
            if filters: tool["filters"] = filters
            query = _query(args)
            if args.get("freshness"): query += " (published in the past " + args["freshness"] + ")"
            search_label, prompt_label = "web search", "Use web search to find"
        result_contract = (
            f" Return only JSON with an answer string and a results array of at most {args['max_results']} objects."
            f" Each result must contain title and URL for the same source returned by {search_label}."
            " Include snippet only when a snippet or description is available from that same source;"
            " copy it faithfully, otherwise omit snippet. Do not invent snippets or URLs."
            ' Use exactly this shape: {"answer":"...","results":[{"title":"...","url":"https://...","snippet":"..."}]}.'
        )
        payload = {"model": model, "stream": True, "reasoning": {"effort": "low"},
                   "input": [{"role": "user", "content": prompt_label + ": " + query + result_contract}],
                   "tools": [tool], "max_output_tokens": 1200}
        if not is_x_search:
            payload.update(tool_choice="required", max_tool_calls=1)
        with oauth_manager.account_generation_guard(state_key) as current:
            if not current:
                raise SearchError("搜索账户身份已变更", code="search_account_retired",
                                  status_code=503, retryable=False)
        from .channel.xai_oauth_channel import XAIOAuthChannel
        channel = XAIOAuthChannel(account)
        channel.state_key = state_key
        if backend.get("endpoint"):
            channel.base_url = str(backend["endpoint"]).rstrip("/")
        upstream = await channel.build_upstream_request(payload, model, ingress_protocol="responses")
        url, headers, payload = upstream.url, upstream.headers, upstream.body
    else:
        if operation == "extract":
            raise SearchError("Anthropic网页提取尚未验证", code="search_capability_unavailable", retryable=False)
        # Reuse the application's CC OAuth identity, beta policy and signed body.
        # CPA preserves native web_search_* (and omits ambiguous empty domains).
        from .channel.oauth_channel import OAuthChannel
        channel = OAuthChannel(account)
        channel.state_key = state_key
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        if args["allowed_domains"]: tool["allowed_domains"] = args["allowed_domains"]
        if args["blocked_domains"]: tool["blocked_domains"] = args["blocked_domains"]
        query = _query(args)
        if args.get("freshness"):
            query += " (published in the past " + args["freshness"] + ")"
        body = {"model": model, "max_tokens": 1600, "stream": False, "tools": [tool],
                "tool_choice": {"type": "tool", "name": "web_search"},
                "messages": [{"role": "user", "content": "Search the web for: " + query}]}
        upstream = await channel.build_upstream_request(body, model)
        url, headers, payload = upstream.url, upstream.headers, upstream.body
    async with network.async_client(timeout=httpx.Timeout(float(cfg["timeoutSeconds"])), follow_redirects=False,
                                    proxy_purpose="oauth_" + ("claude" if kind == "anthropic" else kind),
                                    proxy_channel="oauth:" + key, proxy_model=model) as client:
        with oauth_manager.account_generation_guard(state_key) as current:
            if not current:
                raise SearchError("搜索账户身份已变更", code="search_account_retired", status_code=503, retryable=False)
        if kind == "xai":
            terminal = None
            request_kwargs = ({"content": payload} if isinstance(payload, (str, bytes))
                              else {"json": payload})
            async with client.stream("POST", url, headers=headers, **request_kwargs) as response:
                if response.status_code >= 400:
                    await response.aread()
                    if response.status_code == 429 and operation == "x_search":
                        try:
                            failed_body = response.json()
                        except ValueError:
                            failed_body = None
                        label = "xAI X搜索" if operation == "x_search" else "xAI搜索"
                        raise _billing_failure(SearchError(
                            label + "额度不足或触发速率限制", code="search_rate_limited",
                            status_code=429, retryable=True,
                        ), backend, failed_body)
                    _check_response(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"): continue
                    text = line[5:].strip()
                    if not text or text == "[DONE]": continue
                    event = json.loads(text)
                    if event.get("type") == "response.completed":
                        terminal = event.get("response")
                        break
                    if event.get("type") in ("error", "response.failed", "response.incomplete"):
                        raise _billing_failure(SearchError("xAI搜索未正常完成", code="search_upstream_error"),
                                               backend, event.get("response") or event)
            if not isinstance(terminal, dict):
                raise SearchError("xAI搜索流缺少完成事件", code="incomplete_search_response")
            data = terminal
        else:
            kwargs = {"content": payload} if isinstance(payload, (str, bytes)) else {"json": payload}
            response = await client.post(url, headers=headers, **kwargs)
            try:
                data = _check_response(response)
            except SearchError as exc:
                try:
                    failed_body = response.json()
                except ValueError:
                    failed_body = None
                raise _billing_failure(exc, backend, failed_body) from None
    try:
        return _parse_oauth_result(data, kind, args, operation, cfg)
    except SearchError as exc:
        raise _billing_failure(exc, backend, data) from None
    except Exception:
        error = SearchError("搜索上游认证或响应处理失败", code="search_backend_error", retryable=False)
        raise _billing_failure(error, backend, data) from None


def _parse_oauth_result(data: dict, kind: str, args: dict, operation: str, cfg: dict) -> dict:
    result = {}
    if data.get("usage") is not None: result["usage"] = data["usage"]
    # Private billing evidence for the call log. It is consumed by
    # ``_finish_search_call_success`` and stripped before any model-visible
    # payload is produced, so it never reaches the conversation.
    result["_billing_body"] = data
    if isinstance(data.get("model"), str): result["_upstream_model"] = data["model"]
    if kind == "openai":
        if operation == "extract":
            content = data.get("output")
            if not isinstance(content, str) or not content:
                raise SearchError("OpenAI未返回网页正文", code="empty_extract_response")
            _set_extract_content(result, args["url"], content, cfg)
        else:
            rows = data.get("results")
            if not isinstance(rows, list):
                raise SearchError("OpenAI搜索缺少结构化结果", code="invalid_search_response")
            result.update(query=args["query"], results=_normalize_rows(rows, args))
    elif kind == "xai":
        sources, citations, answer_parts, searched = [], [], [], False
        expected_call = "x_search_call" if operation == "x_search" else "web_search_call"
        for item in data.get("output") or []:
            # The public Responses contract uses x_search_call. The current xAI
            # OAuth Responses wire instead exposes its internal X operations as
            # completed custom_tool_call items; accept both observed native forms.
            native_x_call = operation == "x_search" and is_x_search_call_item(item)
            if item.get("type") == expected_call or native_x_call:
                searched = True
                sources.extend((item.get("action") or {}).get("sources") or [])
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    answer_parts.append(part.get("text") or "")
                for citation in part.get("annotations") or []:
                    if citation.get("type") == "url_citation":
                        citations.append(citation)
        if not searched:
            label = "X搜索" if operation == "x_search" else "网页搜索"
            raise SearchError(f"xAI未执行要求的原生{label}", code="search_not_executed")
        answer_text = "\n".join(answer_parts)
        evidence = citations + sources
        structured = _xai_structured_output(answer_text)
        if structured is None:
            # Older/normal responses can be plain text. Keep their native
            # citation/source fallback usable instead of treating them as errors.
            rows, answer = evidence, answer_text
        else:
            rows, structured_answer = structured
            rows = _xai_aligned_rows(rows, evidence, x_search=operation == "x_search")
            answer = structured_answer if structured_answer is not None else answer_text
        result.update(query=args["query"],
                      results=_normalize_rows(rows, args, omit_missing_snippet=True), answer=answer)
    else:
        rows, answer, searched = [], [], False
        for block in data.get("content") or []:
            if block.get("type") == "web_search_tool_result":
                searched = True
                content = block.get("content")
                if isinstance(content, dict) and content.get("type") == "web_search_tool_result_error":
                    raise SearchError("Anthropic搜索工具执行失败", code="search_upstream_error")
                if isinstance(content, list): rows.extend(content)
            elif block.get("type") == "text":
                answer.append(block.get("text") or "")
                rows.extend({"url": x.get("url"), "title": x.get("title"), "snippet": x.get("cited_text")}
                            for x in block.get("citations") or [] if x.get("url"))
        if not searched:
            raise SearchError("Anthropic未执行要求的搜索", code="search_not_executed")
        result.update(query=args["query"], results=_normalize_rows(rows, args), answer="\n".join(answer))
    return result


def _portable_context(result: dict, args: dict) -> None:
    """Adapt context preference without changing retrieval cost or truncating JSON/URLs."""
    level = args.get("search_context_size")
    if level not in _CONTEXT_CHARS:
        return
    maximum = _CONTEXT_CHARS[level]
    remaining, truncated = maximum, False
    for row in [*(result.get("results") or []), result]:
        for field in ("snippet", "content", "answer"):
            value = row.get(field)
            if not isinstance(value, str):
                continue
            take = min(remaining, len(value))
            row[field] = value[:take]
            remaining -= take
            truncated |= take != len(value)
    result["context_budget"] = {"requested": level, "method": "parrot_returned_text_char_cap", "max_chars": maximum}
    if truncated:
        result["truncated"] = True


async def _run(operation: str, arguments: dict, *, request_id: str | None = None,
               backend_id: str | None = None, origin: str = "managed_round",
               round_no: int = 0) -> dict:
    cfg = settings()
    args = _arguments(arguments, cfg, operation)
    candidates = []
    cached_only = args.get("external_web_access") is False or args.get("external_web_access") in ("cached", "indexed")
    for backend in cfg.get("backends") or []:
        kind = backend.get("type")
        if kind not in BACKEND_TYPES or backend.get("enabled", True) is False:
            continue
        if backend_id and backend.get("id") != backend_id:
            continue
        if cached_only and kind != "openai":
            continue
        if operation == "extract" and kind not in EXTRACT_TYPES:
            continue
        if operation == "x_search" and kind != "xai":
            continue
        credentials = _credentials(backend)
        candidates.extend((backend, credential, position) for position, credential in enumerate(credentials))
    if not candidates:
        if cached_only:
            raise SearchError("没有支持离线/缓存搜索的可用OpenAI来源；未发送在线搜索请求", code="offline_search_unavailable", status_code=503, retryable=False)
        if operation == "x_search":
            raise SearchError("X（Twitter）搜索当前没有可用的xAI OAuth账户",
                              code="no_x_search_backend", status_code=503, retryable=False)
        raise SearchError("没有已配置且可用的搜索来源", code="no_search_backend", status_code=503, retryable=False)
    attempts, permanent = [], set()
    maximum = max(1, min(10, int(cfg["maxAttempts"])))
    cursor = 0
    last = None
    call_id = uuid.uuid4().hex
    for attempt_no in range(1, maximum + 1):
        eligible = [i for i in range(len(candidates)) if i not in permanent]
        if not eligible: break
        index = next((i for i in eligible if i >= cursor), eligible[0])
        cursor = (index + 1) % len(candidates)
        backend, credential, credential_position = candidates[index]
        started = time.monotonic()
        # One row per real upstream call: source/credential/outcome/cost are all
        # recorded, because a single search may legitimately hit several sources.
        log_handle = _record_search_call_start(
            call_id=call_id, attempt_no=attempt_no, origin=origin, request_id=request_id,
            round_no=round_no, backend=backend, credential=credential,
            credential_position=credential_position,
            operation=operation, args=args,
        )
        attempt = {"backend_id": backend["id"], "provider": backend["type"]}
        try:
            async with asyncio.timeout(float(cfg["timeoutSeconds"])):
                if backend["type"] == "zhipu":
                    from .search_zhipu import adapter
                    result = await adapter(backend, credential, args, operation, cfg)
                elif backend["type"] in ENDPOINTS:
                    result = await _http_adapter(backend, credential, args, operation, cfg)
                else:
                    result = await _oauth_adapter(backend, credential, args, operation, cfg)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            attempt.update(status="success", elapsed_ms=elapsed_ms)
            attempts.append(attempt)
            _finish_search_call_success(
                log_handle, backend=backend, result=result, operation=operation,
                args=args, elapsed_ms=elapsed_ms,
            )
            # Private billing evidence must not travel with the public result.
            for _private in ("_billing_body", "_upstream_model"):
                result.pop(_private, None)
            result.update(provider=backend["type"], backend_id=backend["id"], attempts=attempts)
            warnings = []
            if operation == "search" and backend["type"] != "openai":
                if args.get("user_location"):
                    warnings.append("approximate_user_location_applied_to_query_not_a_native_location_control")
                if args.get("search_context_size"):
                    _portable_context(result, args)
                    warnings.append("search_context_size_adapted_to_returned_text_char_budget_not_native_context_tokens")
            if operation == "search" and (args.get("language") or args.get("country")) and backend["type"] != "brave":
                warnings.append("locale_preferences_applied_to_query_not_a_hard_filter")
            if operation == "search" and args.get("freshness") and backend["type"] in ("xai", "anthropic", "anysearch", "zhipu"):
                warnings.append("freshness_is_a_backend_preference_not_a_verified_hard_filter")
            if warnings:
                result.setdefault("warnings", []).extend(warnings)
            return result
        except (TimeoutError, httpx.TimeoutException):
            last = SearchError("搜索调用超时", code="search_timeout", status_code=504)
        except SearchError as exc:
            last = exc
            if exc.code == "url_not_allowed":
                # A policy refusal is not a backend outage to bypass elsewhere.
                _finish_search_call_failure(log_handle, last, started)
                for field in ("_billing_body", "_billing_model", "_billing_provider"):
                    exc.__dict__.pop(field, None)
                raise
            # A same-request immediate retry cannot clear a quota/rate limit.
            # Retire this account for this call while retaining retryability for
            # a later user call; the loop can still try another eligible account.
            if exc.code == "search_rate_limited" or not exc.retryable:
                permanent.add(index)
        except httpx.RequestError:
            last = SearchError("搜索上游网络错误", code="search_network_error")
        except asyncio.CancelledError:
            _finish_search_call_failure(log_handle, SearchError("已取消", code="cancelled", retryable=False), started)
            raise
        except Exception:
            # Exceptions may embed tokens/URLs/account identities. Never relay them.
            last = SearchError("搜索上游认证或响应处理失败", code="search_backend_error", retryable=False)
            permanent.add(index)
        _finish_search_call_failure(log_handle, last, started)
        attempt.update(status="error", code=last.code, elapsed_ms=round((time.monotonic() - started) * 1000))
        attempts.append(attempt)
    assert last is not None
    error = SearchError(f"{last.message}（总尝试 {len(attempts)} 次）", code=last.code,
                        status_code=last.status_code, retryable=last.retryable)
    error.attempts = attempts
    raise error


def _record_search_call_start(*, call_id, attempt_no, origin, request_id, round_no,
                              backend, credential, credential_position, operation, args):
    """Open one search-call row; logging must never break the search itself."""
    try:
        from . import log_db
        if isinstance(credential, dict):
            credential_kind = "oauth"
            account_key = ""
            try:
                from .oauth_ids import account_key as _ak
                account_key = _ak(credential)
            except Exception:
                account_key = ""
            credential_label = account_key or "oauth"
            credential_index = None
            model = str(backend.get("model") or "")
        else:
            credential_kind = "api_key"
            credential_label = "Key #" + str(int(credential_position) + 1)
            credential_index = int(credential_position)
            account_key = ""
            model = ""
        return log_db.record_search_call(
            call_id=call_id, attempt_no=attempt_no, origin=origin,
            request_id=request_id, round_no=round_no,
            source_id=str(backend.get("id") or ""), source_type=str(backend.get("type") or ""),
            source_name=str(backend.get("name") or ""), operation=operation,
            credential_kind=credential_kind, credential_label=credential_label,
            account_key=account_key, credential_index=credential_index,
            model=model,
            query=args.get("query") if operation in ("search", "x_search") else None,
            url=args.get("url") if operation == "extract" else None,
        )
    except Exception:
        return None


def _finish_search_call_success(handle, *, backend, result, operation, args, elapsed_ms):
    """Settle one successful search call, including its model billing facts."""
    if handle is None:
        return
    try:
        from . import log_db
        rows = result.get("results") or []
        content = result.get("content") or result.get("answer") or ""
        model = _effective_model(backend, result)
        log_db.finish_search_call(
            handle, status="success", elapsed_ms=elapsed_ms,
            result_count=len(rows) if isinstance(rows, list) else 0,
            content_chars=len(content) if isinstance(content, str) else 0,
            response_body=result.get("_billing_body"),
            model=model or None, provider=str(backend.get("type") or "") or None,
        )
    except Exception:
        pass


def _finish_search_call_failure(handle, error, started):
    """Settle one failed search call; latency is still an observed fact."""
    if handle is None:
        return
    try:
        from . import log_db
        log_db.finish_search_call(
            handle, status="error",
            error_code=getattr(error, "code", None),
            elapsed_ms=round((time.monotonic() - started) * 1000),
            response_body=getattr(error, "_billing_body", None),
            model=getattr(error, "_billing_model", None),
            provider=getattr(error, "_billing_provider", None),
        )
    except Exception:
        pass


def _effective_model(backend: dict, result: dict) -> str:
    """Report the model actually used, not the hardcoded fallback in isolation."""
    explicit = str(backend.get("model") or "")
    if explicit:
        return explicit
    claimed = result.get("_upstream_model")
    if isinstance(claimed, str) and claimed.strip():
        return claimed.strip()
    kind = str(backend.get("type") or "")
    return {"openai": "gpt-5.5", "xai": "grok-4.6", "anthropic": "claude-sonnet-4-6"}.get(kind, "")


async def search(arguments: dict, *, request_id: str | None = None, backend_id: str | None = None,
                 origin: str = "managed_round", round_no: int = 0) -> dict:
    return await _run("search", arguments, request_id=request_id, backend_id=backend_id,
                      origin=origin, round_no=round_no)


async def x_search(arguments: dict, *, request_id: str | None = None, backend_id: str | None = None,
                   origin: str = "mcp", round_no: int = 0) -> dict:
    """Search X through every eligible xAI backend/account, preserving normal failover."""
    return await _run("x_search", arguments, request_id=request_id, backend_id=backend_id,
                      origin=origin, round_no=round_no)


async def extract(arguments: dict, *, request_id: str | None = None, backend_id: str | None = None,
                  origin: str = "managed_round", round_no: int = 0) -> dict:
    return await _run("extract", arguments, request_id=request_id, backend_id=backend_id,
                      origin=origin, round_no=round_no)
