"""Exact, incarnation-bound targets and safe readable snapshots (no credential copies)."""
import hashlib
from dataclasses import replace
from .events import Subject
from .providers import REGISTRY


def _short(value):
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _text(value):
    return str(value or "").strip()[:160]


def account(account_or_key, *, expected_generation=None):
    from .. import config, oauth_ids, channel_state
    accounts = config.get().get("oauthAccounts") or []
    if isinstance(account_or_key, dict):
        a = account_or_key
        key = oauth_ids.account_key(a)
    else:
        key = str(account_or_key)
        # Never legacy email resolution, nor provider guesses for a missing target.
        a = next((a for a in accounts if oauth_ids.account_key(a) == key), None)
        if a is None:
            return Subject("account", key, expected_generation or "missing", "", "未注册服务", _short(key))
    try:
        generation = channel_state.register_oauth_generation("oauth:" + key, a.get("generationId"))
    except ValueError:
        generation = "conflict"
    if expected_generation and generation != expected_generation:
        return Subject("account", key, expected_generation, "", "原账户", _short(expected_generation))
    provider_id = str(a.get("provider") or "claude")
    provider = REGISTRY.get(provider_id)
    service = (provider.variant(a) if provider.variant else provider.name) if provider else "未注册服务 · " + _text(provider_id)
    identity = next((_text(a.get(k)) for k in (provider.identity_fields if provider else ("email", "nickname")) if a.get(k)), "")
    label = _text(a.get("label"))
    scopes = []
    for field, name in (provider.scopes if provider else ()):
        value = _text(a.get(field))
        if value and name not in {p[0] for p in scopes}:
            scopes.append((name, value))
    # Missing human names must not erase important scopes or dump UUIDs in the body.
    for name, fields in (("工作区", ("workspace_id", "chatgpt_account_id")), ("组织", ("organization_id",)), ("项目", ("project_id",)), ("企业", ("enterprise_id",))):
        if name not in {p[0] for p in scopes} and any(a.get(k) for k in fields):
            scopes.append((name, "未命名 · #" + _short(generation + name)))
    plan = _text(provider.plan(a) if provider and provider.plan else a.get("plan_type"))
    settings = config.get().get("notifications") or {}
    if settings.get("maskIdentity") and "@" in identity:
        local, domain = identity.split("@", 1)
        identity = local[:2] + "***@" + domain
    return Subject("account", key, generation, provider_id, service, _short(generation), label, identity, tuple(scopes), plan)


def channel(key):
    from .. import channel_state, config
    resolved = channel_state.resolve(str(key))
    expected = str(key) if str(key).startswith(("oauth-generation:", "api-generation:")) else None
    if resolved.startswith("oauth:"):
        return account(resolved[6:], expected_generation=expected)
    entry = next((a for a in config.get().get("channels") or [] if "api:" + str(a.get("name")) == resolved), None)
    if not entry:
        return Subject("channel", resolved, "missing", "", "自定义 API 渠道", _short(resolved))
    try:
        generation = channel_state.register_api_generation(resolved, entry.get("generationId"))
    except ValueError:
        generation = "conflict"
    if expected and generation != expected:
        return Subject("channel", resolved, expected, "", "原渠道", _short(expected))
    provider_id = str(entry.get("providerId") or entry.get("provider") or "")
    provider = REGISTRY.get(provider_id)
    return Subject("channel", resolved, generation, provider_id, provider.name if provider else "自定义 API 渠道", _short(generation), _text(entry.get("name")))


def system(name="responses_store", *, kind="system"):
    return Subject(kind, name, "v1", "parrot", "Parrot", _short(name),
                   "Responses历史存储" if name == "responses_store" else _text(name))


def current(subject):
    """Return exact current target only if the SAME generation survives, including rename."""
    from .. import config, oauth_ids, channel_state
    s = subject if isinstance(subject, dict) else subject.__dict__
    if s["kind"] not in {"account", "channel"}:
        return {}
    if s["generation"] in {"missing", "conflict"} or channel_state.is_deleted(s["generation"]):
        return None
    entries = config.get().get("oauthAccounts" if s["kind"] == "account" else "channels") or []
    matches = []
    for item in entries:
        key = oauth_ids.account_key(item) if s["kind"] == "account" else "api:" + str(item.get("name"))
        register = channel_state.register_oauth_generation if s["kind"] == "account" else channel_state.register_api_generation
        try:
            generation = register("oauth:" + key if s["kind"] == "account" else key, item.get("generationId"))
        except ValueError:
            return None  # ambiguous identity must never become a writable first match
        if generation == s["generation"]:
            matches.append({"key": key, "account": item})
    return matches[0] if len(matches) == 1 else None
