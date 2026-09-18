"""Read-only media configuration compatibility; writes belong to management CAS.

Models and image/video purpose/cache policy are independent. Legacy shared values
are inherited only while the new domain is absent, never written by a read.
"""
from __future__ import annotations
import copy
from . import config, oauth_manager, channel_state
from .oauth_ids import account_key

IMAGE_DEFAULT_MODELS = {'openai': ['gpt-image-2', 'gpt-image-2.5', 'gpt-image-2.5-sunburst', 'gpt-image-2.5-flare'], 'xai': ['grok-imagine-image', 'grok-imagine-image-quality']}
VIDEO_DEFAULT_MODELS = {'xai': ['grok-imagine-video', 'grok-imagine-video-1.5']}
CACHE_FIELDS = ('cacheEnabled', 'cachePath', 'cacheRetentionDays', 'cacheMaxBytes')


def section(kind: str) -> str:
    if kind not in ('image', 'video'): raise ValueError('media kind must be image or video')
    return 'images' if kind == 'image' else 'videos'


def _names(values) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values or [] if isinstance(value, str) and value.strip()))


def model_map(kind: str, root: dict | None = None) -> dict[str, list[str]]:
    root = config.get() if root is None else root
    field = kind + '_models'
    if isinstance(root.get(field), dict):
        return {str(provider): _names(values) for provider, values in root[field].items() if isinstance(values, list)}
    defaults = copy.deepcopy(IMAGE_DEFAULT_MODELS if kind == 'image' else VIDEO_DEFAULT_MODELS)
    legacy = root.get('xaiOAuth') or {}
    old_field = 'imageModels' if kind == 'image' else 'videoModels'
    if old_field in legacy:
        defaults['xai'] = _names(legacy[old_field])
    if kind == 'image':
        old = str((root.get('images') or {}).get('toolModel') or '').strip()
        if old and old not in defaults['openai']: defaults['openai'].append(old)
    return defaults


def account_models(account: dict, kind: str, root: dict | None = None) -> list[str]:
    field = 'imageModels' if kind == 'image' else 'videoModels'
    # Existing account-scoped lists are explicit overrides, not discarded by migration.
    if field in account: return _names(account[field])
    return model_map(kind, root).get(account.get('provider', 'anthropic'), [])


def materialize_models(root: dict, kind: str) -> None:
    root[kind + '_models'] = model_map(kind, root)
    if kind == 'image':
        root.setdefault('images', {}).pop('toolModel', None)
        root['images'].pop('mainModel', None)
    (root.get('xaiOAuth') or {}).pop('imageModels' if kind == 'image' else 'videoModels', None)


def settings(kind: str, root: dict | None = None) -> dict:
    root = config.get() if root is None else root
    image_defaults = config.DEFAULT_CONFIG.get('images') or {}
    image = {**image_defaults, **(root.get('images') or {})}
    if kind == 'image':
        effective = dict(image)
    else:
        old = root.get('xaiOAuth') or {}
        effective = {key: copy.deepcopy(image.get(key)) for key in CACHE_FIELDS}
        effective.update(enabled=True, requestTimeoutSeconds=old.get('mediaRequestTimeoutSeconds', 180),
                         jobTtlSeconds=old.get('videoJobTtlSeconds', 10800))
        effective.update(root.get('videos') or {})
    effective.pop('mainModel', None); effective.pop('toolModel', None)
    effective['_media_kind'] = kind
    return effective


def freeze_cache_inheritance(root: dict, kind: str) -> None:
    """Save the OTHER domain's inherited values before changing the shared ancestor."""
    if kind == 'image':
        video = settings('video', root)
        target = root.setdefault('videos', {})
        for key in CACHE_FIELDS:
            target.setdefault(key, copy.deepcopy(video.get(key)))


def oauth_state(account: dict, kind: str, root: dict | None = None) -> dict:
    root = config.get() if root is None else root
    cfg = settings(kind, root)
    state_key = oauth_manager.account_state_key(account)
    ordinary = bool(account.get('enabled', True)) and not account.get('disabled_reason')
    manual = not bool(account.get('enabled', True)) and account.get('disabled_reason') == 'user'
    independent = state_key in (cfg.get('independentAccounts') or [])
    excluded = state_key in (cfg.get('disabledSources') or [])
    if kind == 'image' and account.get('provider') == 'openai':
        key = account_key(account)
        aliases = {key.lower(), ('oauth:' + key).lower(), str(account.get('email', '')).lower(), 'openai:' + str(account.get('email', '')).lower()}
        excluded |= bool(aliases & {str(item).lower() for item in cfg.get('disabledAccounts') or []})
    identity_missing = account.get('provider') == 'openai' and not (account.get('workspace_id') or account.get('chatgpt_account_id'))
    credentials_missing = not (account.get('access_token') or account.get('refresh_token'))
    retired = channel_state.is_deleted(state_key)
    eligible = (ordinary or manual) and not (identity_missing or credentials_missing or retired)
    purpose = not excluded and (ordinary or (manual and independent))
    noun = '图片' if kind == 'image' else '视频'
    reason = None
    if retired: reason = '账户已删除，旧身份授权不能复用。'
    elif identity_missing or credentials_missing: reason = '账户身份或凭据缺失，请重新登录；用途开关不能绕过。'
    elif not ordinary and not manual: reason = '账户认证/配额异常或非用户停用；不能绕过，请先在账户管理中修复。'
    elif not purpose: reason = f'{noun}用途已停用；{noun}独立启用仅授权本用途，不启用对话或另一媒体用途。'
    elif not cfg.get('enabled', True): reason = f'{noun}接口已关闭，请开启接口。'
    return dict(state_key=state_key, oauth_enabled=ordinary, independent_enabled=independent,
                independent_allowed=eligible, purpose_enabled=purpose, image_enabled=not excluded,
                missing_account_id=identity_missing, enabled=reason is None,
                effective_available=reason is None, unavailable_reason=reason)
