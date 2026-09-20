"""Pure xAI X Search wire helpers shared by the unified search service."""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlsplit

X_SEARCH_ONLY_FIELDS = (
    "allowed_x_handles", "excluded_x_handles", "from_date", "to_date",
    "enable_image_understanding", "enable_video_understanding",
)

X_SEARCH_CALL_NAMES = frozenset({
    "x_user_search", "x_keyword_search", "x_semantic_search", "x_thread_fetch",
})


def is_x_search_call_item(item: Any) -> bool:
    """Recognize public and observed xAI-internal X Search history items.

    A normal Responses ``custom_tool_call`` is identified by its client-visible
    ``input``.  xAI's currently observed server-side X operations reuse that item
    type and one of the private names above, but have no client-executable input.
    Requiring the complete observed shape avoids stealing an ordinary custom tool
    merely because its user-selected name happens to match an xAI internal name.
    """
    if not isinstance(item, dict):
        return False
    if item.get("type") == "x_search_call":
        return True
    return bool(
        item.get("type") == "custom_tool_call"
        and item.get("name") in X_SEARCH_CALL_NAMES
        and item.get("status") == "completed"
        and "input" not in item
        and not item.get("namespace")
    )


def normalize_x_search_date(value: Any, field: str) -> tuple[str | None, date | None]:
    """Return one xAI wire date, rejecting ISO8601 datetimes and invalid dates."""
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO8601 YYYY-MM-DD date")
    text = value.strip()
    if len(text) != 10 or text[4:5] != "-" or text[7:8] != "-":
        raise ValueError(f"{field} must be an ISO8601 YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be an ISO8601 YYYY-MM-DD date") from None
    # date.fromisoformat accepts only zero-padded calendar dates here; retaining
    # the canonical value makes every request authored by Parrot wire-compatible.
    return parsed.isoformat(), parsed


def normalize_x_search_tool_dates(tool: dict[str, Any]) -> dict[str, Any]:
    """Validate/canonicalize date fields on a native Responses x_search tool."""
    if tool.get("type") != "x_search":
        return tool
    out = dict(tool)
    parsed_dates: dict[str, date] = {}
    for field in ("from_date", "to_date"):
        if field in out:
            normalized, parsed = normalize_x_search_date(out[field], field)
            if normalized is None:
                out.pop(field, None)
            else:
                out[field] = normalized
                assert parsed is not None
                parsed_dates[field] = parsed
    if (parsed_dates.get("from_date") and parsed_dates.get("to_date")
            and parsed_dates["from_date"] > parsed_dates["to_date"]):
        raise ValueError("from_date must not be later than to_date")
    return out


def evidence_key(url: str, *, x_search: bool) -> str:
    """Match x.com citation aliases without weakening ordinary web URL evidence."""
    if not x_search:
        return url
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.removeprefix("www.") not in ("x.com", "twitter.com"):
        return url
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[-2].lower() == "status" and parts[-1].isdigit():
        return "x-status:" + parts[-1]
    if len(parts) == 1 and parts[0].lower() not in {"home", "explore", "search", "i"}:
        return "x-profile:" + parts[0].casefold()
    return url
