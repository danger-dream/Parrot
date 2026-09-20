"""One image inventory for routing, model-center and discovery."""
from __future__ import annotations
from dataclasses import dataclass
from . import config, model_state, model_metadata, oauth_manager, channel_state, media_config
from .oauth_ids import account_key

@dataclass(frozen=True)
class ImageSource:
    model: str
    upstream: str
    provider: str
    key: str
    label: str
    enabled: bool
    source_enabled: bool
    state_key: str | None = None
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and self.source_enabled and model_state.is_global_enabled(self.model)


def is_image_name(name: str) -> bool:
    value = str(name).lower()
    return value.startswith(('gpt-image-', 'dall-e-', 'grok-imagine-image')) or '-image' in value or value.startswith('imagen-')


def openai_account_state(account: dict, cfg: dict) -> dict:
    state = media_config.oauth_state(account, 'image', cfg)
    models = media_config.account_models(account, 'image', cfg)
    available = any(model_state.is_global_enabled(model, cfg) and model_state.is_source_enabled('oauth:' + account_key(account), model, cfg) for model in models)
    if state['enabled'] and not available:
        state['effective_available'] = False
        state['unavailable_reason'] = '本来源没有启用的图片模型；请通过配置或管理 API 维护。'
    return state


def oauth_source_label(account: dict, cfg: dict) -> str:
    """Use the existing human workspace convention; never show an opaque ID."""
    provider = account.get('provider', 'anthropic')
    base = str(account.get('label') or account.get('email') or provider)
    if provider != 'openai': return base
    same_email = [item for item in cfg.get('oauthAccounts') or []
                  if item.get('provider') == 'openai' and item.get('email') == account.get('email')]
    if len(same_email) <= 1: return base
    name = str(account.get('workspace_name') or '').strip()
    kind = str(account.get('workspace_type') or '').strip()
    plan = str(account.get('plan_type') or '').strip()
    # Match the OAuth menu: a Team workspace called Personal is not personal.
    workspace = name if name and not (name.lower() == 'personal' and 'team' in f'{kind} {plan}'.lower()) else kind or 'workspace'
    return base if base == workspace else f'{base} · {workspace}'


def sources(cfg: dict | None = None, *, kind: str = 'image') -> list[ImageSource]:
    cfg = config.get() if cfg is None else cfg
    images = media_config.settings(kind, cfg)
    enabled = bool(images.get('enabled', True))
    out = []
    for entry in (cfg.get('channels') or []) if kind == 'image' else []:
        if entry.get('protocol') not in ('openai-chat', 'openai-responses'):
            continue
        key = 'api:' + entry['name']
        for item in entry.get('models') or []:
            if not isinstance(item, dict): continue
            real = item.get('real', '')
            model = item.get('alias') or real
            binding = model_metadata.resolve_binding(model, scope_key=key, outbound_model=real)
            metadata = binding.metadata if binding else {}
            modalities = metadata.get('modalities') or {}
            image_output = isinstance(modalities, dict) and 'image' in (modalities.get('output') or [])
            if not (item.get('kind') == 'image' or image_output or is_image_name(real)): continue
            generation = channel_state.register_api_generation(key, entry.get('generationId'))
            out.append(ImageSource(model, real, entry.get('providerId') or 'openai', key,
                entry['name'], enabled and bool(entry.get('enabled', True)) and not entry.get('disabled_reason') and generation not in (images.get('disabledSources') or []),
                model_state.is_source_enabled(key, model, cfg), generation))
    for account in cfg.get('oauthAccounts') or []:
        provider = account.get('provider', 'anthropic')
        if provider not in (('openai', 'xai', 'antigravity') if kind == 'image' else ('xai',)): continue
        key = 'oauth:' + account_key(account)
        label = oauth_source_label(account, cfg)
        state = media_config.oauth_state(account, kind, cfg)
        active = state['enabled']
        state_key = state['state_key']
        unavailable_reason = state['unavailable_reason']
        models = media_config.account_models(account, kind, cfg)
        for model in models:
            out.append(ImageSource(model, model, provider, key, label, bool(active), model_state.is_source_enabled(key, model, cfg), state_key, unavailable_reason))
    return out


def current_oauth_account(source: ImageSource) -> tuple[str, dict]:
    """Revalidate a selected OAuth generation/authorization before token work or I/O."""
    import copy
    if not source.state_key:
        raise ValueError('image source has no selected account generation')
    with oauth_manager.account_generation_guard(source.state_key) as current:
        if not current:
            raise ValueError('selected image account generation was deleted; select the current source again')
        key = channel_state.resolve(source.state_key)
        account_id = key.removeprefix('oauth:')
        account = oauth_manager.get_account(account_id)
        state = media_config.oauth_state(account, 'image', config.get())
        if not state['enabled']:
            raise ValueError(state['unavailable_reason'])
        if not model_state.is_global_enabled(source.model) or not model_state.is_source_enabled(key, source.model):
            raise ValueError('image model/source is disabled; enable it in model center')
        return account_id, copy.deepcopy(account)


# Kept for internal callers and extensions written against the first unified
# image runtime. The implementation has always been provider-neutral.
current_openai_account = current_oauth_account


def models(cfg: dict | None = None, *, kind: str = 'image') -> set[str]:
    """Configured names, including API mappings and account overrides, not just seeds."""
    cfg = config.get() if cfg is None else cfg
    return {row.model for row in sources(cfg, kind=kind)} | {model for values in media_config.model_map(kind, cfg).values() for model in values}


def available_models() -> list[str]:
    return sorted({row.model for row in sources() if row.available and model_state.is_discovery_visible(row.model)})
