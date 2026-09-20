"""Forward upgrade from real v0.32.2 persistence, never current defaults.

Run via src/tests/isolated_pytest.py. All credentials are synthetic; the old
subprocess disables sockets and starts no server, DB or background lifecycle.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from src import config, auth, media_config, model_mapping, model_metadata, model_pricing, model_state, oauth_manager, search_service
from src.channel import registry
from src.tests.test_model_center_rollback import _old_source


@pytest.fixture(scope="module")
def released_config(tmp_path_factory):
    # TemporaryDirectory cleans even if a test fails; pytest temp roots may be retained.
    with tempfile.TemporaryDirectory(dir=tmp_path_factory.getbasetemp(), prefix="release-config-") as directory:
        root = Path(directory)
        old = _old_source(root)
        data = root / "data"
        data.mkdir()
        path = data / "config.json"
        result = root / "release-result.json"
        env = os.environ.copy()
        env.update(ANTHROPIC_PROXY_CONFIG=str(path), ANTHROPIC_PROXY_DATA_DIR=str(data),
                   PYTHONPATH=str(old), PYTHONDONTWRITEBYTECODE="1")
        script = r'''
import json, socket, sys
from pathlib import Path
# Fail closed before importing any project module, including the old loader.
def blocked(*args, **kwargs):
    raise AssertionError("old-release sample generation must not use the network")
class NoNetworkSocket(socket.socket):
    connect = connect_ex = sendto = blocked
socket.socket = NoNetworkSocket
socket.create_connection = socket.getaddrinfo = blocked
from src import __version__, config, auth, model_metadata, model_pricing, model_mapping
from src.channel import registry
from src.openai.channel.registration import register_factories
from src.cursor_bridge import catalog as cursor_catalog
from src.cursor_bridge.models import CursorModel
from src.oauth.workbuddy import auth as workbuddy_auth
cfg = config.get()  # Actually create the released defaults using released code.
def customize(c):
    c['telegram'].update(botToken='', adminIds=[])
    c['notifications']['enabled'] = False
    c['oauth']['mockMode'] = True
    c['apiKeys'] = {
        'string-key': 'fixture-string',
        'restricted': {'key': 'fixture-restricted', 'enabled': True,
                       'allowedModels': ['client-alias', 'grok-imagine-custom'],
                       'allowImages': True, 'allowVideos': False,
                       'limits': {'maxConcurrent': 2}},
        'disabled': {'key': 'fixture-disabled', 'enabled': False,
                     'allowedModels': [], 'allowImages': False, 'allowVideos': True},
    }
    c['channels'] = [{
        'name': 'release-api', 'type': 'api', 'enabled': True,
        'protocol': 'anthropic', 'baseUrl': 'https://fixture.invalid',
        'apiKey': 'fixture-upstream', 'maxConcurrent': 3,
        'models': [{'alias': 'client-alias', 'real': 'claude-sonnet-4-5'},
                   {'alias': 'claude-sonnet-4-5', 'real': 'claude-sonnet-4-5'}],
    }]
    accounts = []
    for provider, model in [('claude', 'claude-sonnet-4-5'), ('openai', 'gpt-5.4'),
                            ('xai', 'grok-4.5'), ('antigravity', 'gemini-3-flash')]:
        accounts.append({
            'provider': provider, 'email': provider + '@example.invalid',
            'subject': 'fixture-subject', 'workspace_id': 'fixture-workspace',
            'project_id': 'fixture-project', 'enabled': True,
            'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh',
            'expired': '2999-01-01T00:00:00Z',
            'models': [model, 'disabled-model'], 'disabledModels': ['disabled-model'],
            'account_model_catalog': {'schema': 1, 'models': [
                {'id': model, 'contextWindow': 128000, 'maxOutputTokens': 16000},
                {'id': 'disabled-model'}]},
            'last_model_sync': '2026-09-01T00:00:00Z', 'last_model_sync_source': 'upstream:' + provider,
        })
    cursor = cursor_catalog.build_catalog([
        CursorModel(id='composer-2.5', name='Composer 2.5', context_window=200000,
                    max_tokens=64000, supports_agent=True, default_on=True,
                    reasoning=True, supports_images=False, supports_max_mode=True),
    ], fetched_at='2026-09-01T00:00:00Z')
    accounts.append({'provider': 'cursor', 'type': 'cursor', 'email': 'cursor@example.invalid',
                     'subject': 'fixture-cursor', 'access_token': 'fixture-cursor-access',
                     'enabled': True, 'models': ['composer-2.5'], 'cursor_model_catalog': cursor,
                     'cursor_disabled_models': []})
    workbuddy = workbuddy_auth.normalize_credential({'realm': 'cn', 'uid': 'fixture-workbuddy',
                    'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh'})
    workbuddy.update(models=['glm-fixture'], account_model_catalog={'schema': 1, 'models': [
        {'id': 'glm-fixture', 'reasoningEfforts': ['low', 'high'], 'maxInputTokens': 100000}]})
    accounts.append(workbuddy)
    accounts.append({'provider': 'claude', 'email': 'empty@example.invalid',
                     'enabled': True, 'access_token': 'fixture-empty', 'models': []})
    c['oauthAccounts'] = accounts
    c['oauthDefaultModels'] = ['custom-claude-fallback']
    c['openaiOAuth']['defaultModels'] = ['custom-openai-fallback']
    c['xaiOAuth'].update(defaultModels=['custom-xai-fallback'],
                         imageModels=['grok-imagine-custom'], videoModels=['grok-video-custom'],
                         mediaRequestTimeoutSeconds=71, videoJobTtlSeconds=4321)
    c['antigravityOAuth'].update(defaultModels=['custom-ag-fallback'], imageModels=['gemini-custom-image'])
    accounts[2]['imageModels'] = ['account-image-custom']
    accounts[2]['videoModels'] = []
    c['images'].update(enabled=False, mainModel='custom-main', toolModel='custom-image',
                       disabledAccounts=['openai:openai@example.invalid'],
                       cacheEnabled=True, cacheRetentionDays=9, cacheMaxBytes=456789,
                       requestTimeoutSeconds=67)
    c['anysearch'].update(enabled=False, apiKey='fixture-search',
                          endpoint='https://search.example.invalid/mcp', timeoutSeconds=73,
                          maxResults=3, maxFetchChars=6789, maxToolRounds=7,
                          minQueryChars=4, maxFetchUrlChars=199,
                          requireKnownUrlForFetch=False, maxConcurrentToolCalls=2)
    c['modelMapping'] = {'global': {'public-alias': 'client-alias'},
                          'anthropic': {'old-alias': 'client-alias'}}
    c['ingressDefaultModel'] = {'anthropic': 'client-alias'}
    c['modelBindings'] = {'defaults': {
        'claude-sonnet-4-5': 'xai/grok-4.5',
        'client-alias': {'target': 'anthropic/claude-sonnet-4-5', 'source': 'manual'},
    }, 'scoped': {'api:release-api': {'client-alias': {
        'target': 'xai/grok-4.5', 'outboundModel': 'claude-sonnet-4-5'}}}}
    # Already inert in 0.32.2: old free-form metadata is NOT an active override.
    c['modelMetadata'] = {'claude-sonnet-4-5': {'contextWindow': 1234, 'compressionModel': True}}
    c['pricing'].update(sourceUrl='https://catalog.example.invalid/api.json',
                         modelsUrl='https://catalog.example.invalid/models.json', autoUpdate=False)
config.update(customize)
config.reload()  # Persist released normalizations (including string Key / Codex identity).
model_pricing.initialize()
model_metadata.migrate_legacy_config()
register_factories()
registry._sync_state_db_with_channels = lambda: None
registry.rebuild_from_config()
Path(sys.argv[1]).write_text(json.dumps({
    'version': __version__, 'available': registry.available_models(),
    'metadata_target': model_metadata.resolve_binding('client-alias', scope_key='api:release-api', outbound_model='claude-sonnet-4-5').target,
    'auth': auth.validate({'authorization': 'Bearer fixture-restricted'}),
    'config': config.reload(),
}))
'''
        process = subprocess.run([sys.executable, "-c", script, str(result)], cwd=old, env=env,
                                 capture_output=True, text=True, timeout=60)
        assert process.returncode == 0, process.stdout + process.stderr
        record = json.loads(result.read_text())
        assert record["version"] == "0.32.2"
        assert "modelCenter" not in record["config"]
        assert "modelMetadataOverrides" not in record["config"]
        assert "search" not in record["config"]
        yield record


@pytest.fixture
def upgraded(released_config, tmp_path, monkeypatch):
    raw = copy.deepcopy(released_config["config"])
    # Redirect only storage, not configuration semantics, into this test's root.
    raw['stateDbPath'] = str(tmp_path / 'state.db')
    raw['logDir'] = str(tmp_path / 'logs')
    raw['images']['dbPath'] = str(tmp_path / 'images.db')
    raw['images']['cachePath'] = str(tmp_path / 'cache')
    raw['management']['stateDbPath'] = str(tmp_path / 'management.db')
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0)
    monkeypatch.setattr(config, "_rejected_rewrite_version", None)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(registry, "_channels", {})
    monkeypatch.setattr(registry, "_sync_state_db_with_channels", lambda: None)
    loaded = config.reload()
    return raw, loaded, path


def test_release_first_load_preserves_keys_accounts_channels_and_is_idempotent(upgraded):
    raw, loaded, path = upgraded
    for field in ('apiKeys', 'oauthAccounts', 'channels', 'modelMapping', 'modelBindings',
                  'modelMetadata', 'compressionModel', 'images', 'anysearch', 'pricing',
                  'management', 'ingressDefaultModel'):
        assert loaded[field] == raw[field], field
    assert auth.validate({'authorization': 'Bearer fixture-restricted'}) == (
        'restricted', ['client-alias', 'grok-imagine-custom'], None)
    assert auth.validate({'x-api-key': 'fixture-string'}) == ('string-key', [], None)
    assert auth.validate({'x-api-key': 'fixture-disabled'})[2] == 'API key is disabled'
    assert auth.images_allowed('restricted') and not auth.videos_allowed('restricted')
    assert not auth.images_allowed('string-key') and not auth.videos_allowed('string-key')
    assert all(not auth.mcp_allowed(key) for key in loaded['apiKeys'])
    assert loaded['modelCenter'] == {'schemaVersion': 1, 'disabledModels': [],
                                    'hiddenModels': [], 'apiSourceDisabledModels': {}}
    assert loaded['modelMetadataOverrides'] == {'defaults': {}, 'scoped': {}}
    snapshot = path.read_bytes()
    stat = path.stat().st_mtime_ns
    for _ in range(3):
        assert config.reload() == loaded
        assert path.read_bytes() == snapshot
        assert path.stat().st_mtime_ns == stat
    assert model_metadata.migrate_legacy_config() == {'bindings': 0, 'compression': 0}
    assert path.read_bytes() == snapshot
    # Save/restart also retains stable account identities and all active settings.
    config.save()
    assert config.reload() == loaded


def test_release_routes_aliases_and_retirements_are_explicit(upgraded, released_config):
    raw, loaded, _ = upgraded
    from src.openai.channel.registration import register_factories
    register_factories()
    registry.rebuild_from_config()
    assert set(registry.available_models()) == set(released_config['available']) - {'custom-claude-fallback'}
    channel = registry.get_channel('api:release-api')
    assert channel.supports_model('client-alias') == 'claude-sonnet-4-5'
    assert model_mapping.get_ingress_map('anthropic')['old-alias'] == 'client-alias'
    assert model_mapping.get_global_map()['public-alias'] == 'client-alias'
    assert model_state.is_global_enabled('client-alias')
    assert model_state.is_source_enabled('api:release-api', 'client-alias')
    assert model_state.is_discovery_visible('client-alias')
    assert oauth_manager.account_model_selection(loaded['oauthAccounts'][-1])['models'] == []
    # Accepted contract changes: static fallback retirement, no static-to-LKG copy.
    assert raw['oauthDefaultModels'] == ['custom-claude-fallback']
    assert 'oauthDefaultModels' not in loaded
    for section in ('openaiOAuth', 'xaiOAuth', 'antigravityOAuth'):
        assert 'defaultModels' not in loaded[section]
    assert loaded['ingressDefaultModel'] == raw['ingressDefaultModel']
    from src.model_validation import require_explicit_model, ExplicitModelError
    with pytest.raises(ExplicitModelError):
        require_explicit_model({})


def test_release_media_read_only_inheritance_and_explicit_new_values(upgraded):
    raw, loaded, path = upgraded
    persisted = path.read_bytes()
    image = media_config.settings('image')
    video = media_config.settings('video')
    assert image['enabled'] is False and video['enabled'] is True
    for field in media_config.CACHE_FIELDS:
        assert image[field] == video[field] == raw['images'][field]
    assert image['requestTimeoutSeconds'] == 67
    assert video['requestTimeoutSeconds'] == 71 and video['jobTtlSeconds'] == 4321
    assert media_config.model_map('image')['xai'] == ['grok-imagine-custom']
    assert media_config.model_map('video')['xai'] == ['grok-video-custom']
    assert 'custom-image' in media_config.model_map('image')['openai']
    assert media_config.account_models(loaded['oauthAccounts'][2], 'image') == ['account-image-custom']
    assert media_config.account_models(loaded['oauthAccounts'][2], 'video') == []
    assert 'mainModel' not in image and 'toolModel' not in image
    # The old OpenAI exclusion still applies even if the image endpoint is enabled.
    enabled = copy.deepcopy(loaded)
    enabled['images']['enabled'] = True
    assert not media_config.oauth_state(enabled['oauthAccounts'][1], 'image', enabled)['enabled']
    assert 'image_models' not in loaded and 'video_models' not in loaded
    assert path.read_bytes() == persisted
    explicit = copy.deepcopy(loaded)
    explicit['image_models'] = {'xai': []}
    explicit['video_models'] = {'xai': ['new-video']}
    explicit['videos'] = {'enabled': False, 'cacheEnabled': False, 'cacheRetentionDays': 0}
    assert media_config.model_map('image', explicit) == {'xai': []}
    assert media_config.model_map('video', explicit) == {'xai': ['new-video']}
    assert media_config.settings('video', explicit)['cacheRetentionDays'] == 0
    assert media_config.settings('video', explicit)['cacheEnabled'] is False
    media_config.freeze_cache_inheritance(loaded, 'image')
    loaded['images']['cacheRetentionDays'] = 99
    assert media_config.settings('video', loaded)['cacheRetentionDays'] == 9


def test_release_search_custom_timeout_and_opt_out_survive(upgraded):
    raw, loaded, path = upgraded
    persisted = path.read_bytes()
    search = search_service.settings()
    assert search['functionMode'] == search['hostedMode'] == 'passthrough'
    for field in ('timeoutSeconds', 'maxResults', 'maxFetchChars', 'maxToolRounds',
                  'minQueryChars', 'maxFetchUrlChars', 'requireKnownUrlForFetch', 'maxConcurrentToolCalls'):
        assert search[field] == raw['anysearch'][field], field
    backend = search['backends'][0]
    assert backend['apiKeys'] == ['fixture-search']
    assert backend['endpoint'] == 'https://search.example.invalid'
    assert path.read_bytes() == persisted
    loaded['search'] = {'timeoutSeconds': 19, 'functionMode': 'managed', 'backends': []}
    search = search_service.settings()
    assert search['timeoutSeconds'] == 19
    assert search['functionMode'] == 'managed' and search['backends'] == []


@pytest.mark.parametrize('form', ['string', 'object', 'config-source', 'custom-source'])
def test_release_operator_binding_survives_auto_sync(upgraded, monkeypatch, form):
    raw, loaded, path = upgraded
    # All of these were accepted by released _binding_fields; none is auto-owned.
    binding = 'xai/grok-4.5'
    if form != 'string':
        binding = {'target': binding}
        if form in ('config-source', 'custom-source'):
            binding['source'] = 'config' if form == 'config-source' else 'operator-import'
    loaded['modelBindings']['defaults']['claude-sonnet-4-5'] = binding
    before = copy.deepcopy(binding)
    monkeypatch.setattr(model_pricing, 'canonical_official_model', lambda _: 'anthropic/claude-sonnet-4-5')
    monkeypatch.setattr(model_pricing, 'binding_snapshot', lambda _: {
        'catalogRevision': 'upgrade-test', 'metadata': {'contextWindow': 200000}, 'tariff': None})
    result = model_metadata.sync_auto_snapshots([('claude-sonnet-4-5', None, None)])
    assert result['results'][0]['status'] == 'protected'
    assert loaded['modelBindings']['defaults']['claude-sonnet-4-5'] == before
    assert config.get()['modelBindings']['defaults']['claude-sonnet-4-5'] == before
    scope_before = copy.deepcopy(loaded['modelBindings']['scoped']['api:release-api']['client-alias'])
    result = model_metadata.sync_auto_snapshots([('client-alias', 'api:release-api', 'claude-sonnet-4-5')])
    assert result['results'][0]['status'] == 'protected'
    assert config.get()['modelBindings']['scoped']['api:release-api']['client-alias'] == scope_before


def test_fresh_install_keeps_new_search_deadline(tmp_path, monkeypatch):
    path = tmp_path / 'fresh-config.json'
    monkeypatch.setattr(config, 'CONFIG_PATH', str(path))
    monkeypatch.setattr(config, '_cache', None)
    monkeypatch.setattr(config, '_mtime', 0)
    monkeypatch.setattr(config, '_reload_callbacks', [])
    config.reload()
    assert search_service.settings()['timeoutSeconds'] == 10
    assert config.reload()['search']['timeoutSeconds'] == 10


def test_release_scoped_metadata_binding_and_legacy_migration_survive(upgraded, released_config):
    raw, loaded, _ = upgraded
    model_pricing.initialize()
    binding = model_metadata.resolve_binding('client-alias', scope_key='api:release-api', outbound_model='claude-sonnet-4-5')
    assert binding.target == released_config['metadata_target'] == 'xai/grok-4.5'
    assert loaded['modelBindings'] == raw['modelBindings']
    assert loaded['compressionModel'] == 'claude-sonnet-4-5'
    # Do not revive a free-form value already ignored by the released runtime.
    assert model_metadata.get_metadata('claude-sonnet-4-5').get('contextWindow') != 1234
