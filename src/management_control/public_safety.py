"""Small public-projection helpers for explicitly known secret fields and URLs."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


# Exact vocabulary only: no delimiter normalization, suffix matching, or case
# splitting.  Public identifiers such as key, channelKey, account_key, keyId and
# count/configuration fields therefore need no marker or depth exception.
KNOWN_SENSITIVE_FIELDS = frozenset("""
access-token accessToken access_token api-key api-token apiKey apiToken api_key
api_token apikey botKey botToken bot_key bot_token challengeCredential
challengeSecret challenge_credential challenge_secret clientSecret client_secret
credential exchangeCredential exchangeSecret exchange_credential exchange_secret
githubKey githubToken github_key github_token id-token idToken id_token
managementKey managementToken management_key management_token passwd password
refresh-token refreshToken refresh_token secret session sessionSecret sessionToken
session_secret session_token token upstreamSecret upstream_secret webhookCredential
webhookKey webhookSecret webhookToken webhook_credential webhook_key webhook_secret
webhook_token x-api-key xApiKey x_api_key model_key modelKey zcode_token zcodeToken
poll_token pollToken privateCipher secretKey X-Bigmodel-Authorization
""".split())
# HTTP header names are the sole case-insensitive names in the vocabulary.
_HEADER_FIELDS = frozenset("""
authorization proxy-authorization proxy_authorization proxyauthorization cookie
set-cookie set_cookie setcookie
""".split())


def is_known_sensitive_field(name: object) -> bool:
    """Return whether *name* is one reviewed secret field (not a name pattern)."""
    key = str(name)
    return key in KNOWN_SENSITIVE_FIELDS or key.casefold() in _HEADER_FIELDS


def redact_known_fields(value: Any, *, replacement: str = "[REDACTED]") -> Any:
    """Copy structured data, replacing values of known secret fields only.

    Strings are ordinary business text and are never parsed, scanned, or
    classified. Recursion exists only for actual mappings, lists, and tuples.
    """
    if isinstance(value, Mapping):
        return {
            str(key): replacement if is_known_sensitive_field(key) else redact_known_fields(
                item, replacement=replacement,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_known_fields(item, replacement=replacement) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_known_fields(item, replacement=replacement) for item in value)
    return copy.deepcopy(value)


def split_public_url(value: str) -> SplitResult:
    """Parse a known URL field and force validation of its authority parts."""
    parsed = urlsplit(str(value))
    _ = parsed.hostname, parsed.port, parsed.username, parsed.password
    if not parsed.netloc and "@" in parsed.path:
        raise ValueError("URL could not be safely parsed")
    return parsed


def without_url_userinfo(value: str, *, masked_userinfo: str | None = None) -> str:
    """Remove or explicitly mask userinfo in one value already known to be a URL."""
    parsed = split_public_url(value)
    if not parsed.netloc:
        return str(value)
    if parsed.hostname is None:
        raise ValueError("URL could not be safely parsed")
    if parsed.username is None and parsed.password is None:
        return str(value)
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if not authority:
        raise ValueError("URL could not be safely parsed")
    if masked_userinfo is not None:
        authority = f"{masked_userinfo}@{authority}"
    return urlunsplit(parsed._replace(netloc=authority))
