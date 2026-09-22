"""Model-selected images execution; ambiguous POST outcomes are never retried."""
from __future__ import annotations
import asyncio
import base64
import copy
import json
import time
import uuid
from urllib.parse import urlsplit, urlunsplit
import httpx
from fastapi.responses import JSONResponse
from .. import channel_state, concurrency, config, cooldown, image_artifacts, image_catalog, load_balancing, media_cache, network, oauth_manager, scorer
from ..antigravity import images as antigravity_images
from ..channel import registry
from ..async_owned import await_owned
from ..channel.url_utils import resolve_upstream_url
from ..xai import imagine
from . import images_simple as legacy
from .codex_constants import apply_codex_workspace_routing, codex_responses_url

SAFE_REJECTIONS = {401, 403, 404, 429}
MAX_GENERATIONS = 10


def _prompt(parsed) -> str:
    constraints = []
    if parsed.size and parsed.size != 'auto':
        w, h = image_artifacts.dimensions(parsed.size)
        constraints.append(f'Output canvas {w} by {h} pixels, aspect ratio {w}:{h}; preserve the whole composition.')
    if parsed.native_options.get('background') == 'transparent':
        constraints.append('Background MUST be genuinely transparent with alpha=0, not white or a checkerboard.')
    style = parsed.native_options.get('style')
    if style: constraints.append(f'Image style: {style}.')
    fidelity = parsed.native_options.get('input_fidelity')
    if fidelity == 'high': constraints.append('Preserve reference image identities, text, textures and fine visual details with high fidelity; change only what the user requested.')
    elif fidelity == 'low': constraints.append('Reference images guide the edit; creative visual reinterpretation is allowed.')
    aspect = parsed.xai_options.get('aspect_ratio')
    if aspect and not parsed.size: constraints.append(f'Output aspect ratio {aspect}.')
    if parsed.mask_url:
        constraints.append('Edit only the transparent area indicated by the provided mask. Preserve all other parts of the first image.')
    return parsed.prompt + ('\n\nImage output requirements: ' + ' '.join(constraints) if constraints else '')


def _api_url(channel, action: str) -> str:
    base = channel.base_url.rstrip('/')
    suffix = '/responses' if base.endswith('/v1') else '/v1/responses'
    endpoint = resolve_upstream_url(base, getattr(channel, 'api_path', None), suffix)
    parts = urlsplit(endpoint)
    path = parts.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses', '/images/generations', '/images/edits'):
        if path.endswith(suffix): path = path[:-len(suffix)]; break
    return urlunsplit((parts.scheme, parts.netloc, path+'/images/'+('generations' if action=='generate' else 'edits'), '', ''))


def _aggregate_usage(usages: list) -> dict:
    """Sum only reported, compatible token counters; never derive absent totals.

    Chat-compatible prompt/completion counters are aliases of input/output.
    Vendor billing units and unknown fields remain only in usage_by_call.
    """
    totals = {}
    token_aliases = {'input_tokens': 'prompt_tokens', 'output_tokens': 'completion_tokens', 'total_tokens': 'total_tokens'}
    detail_aliases = {'input_tokens_details': 'prompt_tokens_details', 'output_tokens_details': 'completion_tokens_details'}
    detail_fields = {'cached_tokens', 'text_tokens', 'image_tokens', 'audio_tokens',
                     'reasoning_tokens', 'accepted_prediction_tokens', 'rejected_prediction_tokens'}
    def count(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    for usage in usages:
        if not isinstance(usage, dict): continue
        for field, alias in token_aliases.items():
            value = usage.get(field, usage.get(alias))
            if count(value): totals[field] = totals.get(field, 0) + value
        for field, alias in detail_aliases.items():
            details = usage.get(field, usage.get(alias))
            if not isinstance(details, dict): continue
            for key, value in details.items():
                if key in detail_fields and count(value):
                    target = totals.setdefault(field, {})
                    target[key] = target.get(key, 0) + value
    return totals


def _is_xai_source(source) -> bool:
    return not source.key.startswith('oauth:openai:') and (source.provider == 'xai' or source.upstream.startswith('grok-imagine-image'))


def _uses_api_multipart(source) -> bool:
    return not source.key.startswith('oauth:') and not _is_xai_source(source)


def _xai_image_payload(source, parsed, *, action: str, n: int) -> dict:
    if parsed.native_options.get('background') == 'transparent':
        raise ValueError('xAI Imagine transparent background is not supported by this adapter (verified JPEG output without alpha); use background=auto/opaque or choose a compatible image source')
    if parsed.native_options.get('moderation') not in (None, 'auto'):
        raise ValueError('xAI does not expose moderation; use moderation=auto or choose a compatible image source')
    adapted = copy.copy(parsed)
    adapted.model = source.upstream
    adapted.prompt = _prompt(parsed)
    adapted.requested_n = n
    adapted.response_format = 'b64_json'
    adapted.response_format_explicit = True
    return imagine._build_image_payload(adapted, action=action)


async def _prepare_api_edit_files(parsed) -> list:
    files = []
    for index, ref in enumerate(parsed.input_images):
        raw = await image_artifacts.reference_bytes(ref)
        files.append(('image[]', image_artifacts.multipart_image(raw, f'image-{index}')))
    if parsed.mask_url:
        raw = await image_artifacts.reference_bytes(parsed.mask_url)
        files.append(('mask', image_artifacts.multipart_image(raw, 'mask', force_png=True)))
    return files


async def _send(source, parsed, *, action: str, n: int, cfg: dict) -> httpx.Response:
    """One POST. The caller exclusively owns safe rejection failover."""
    channel = registry.get_channel(source.key)
    if source.key.startswith(('oauth:xai:', 'api:')) and (channel is None or getattr(channel, 'state_key', None) != source.state_key):
        raise ValueError('selected image account generation was deleted')
    payload = {'model': source.upstream, 'prompt': _prompt(parsed), 'n': n}
    options = dict(parsed.native_options)
    for key in ('output_format', 'output_compression', 'style', 'partial_images'):
        options.pop(key, None)  # local format/style adaptation; partials rejected before POST
    headers = {}
    files = None
    if source.key.startswith('oauth:openai:'):
        ak, account = image_catalog.current_openai_account(source)
        token = await oauth_manager.ensure_valid_token(ak, expected_state_key=source.state_key)
        ak, account = image_catalog.current_openai_account(source)
        headers = legacy._build_headers(token, account.get('workspace_id') or account.get('chatgpt_account_id'), source.upstream)
        headers['Accept'] = 'application/json'
        headers['x-codex-image-turn-id'] = str(uuid.uuid4())
        url = codex_responses_url(config.get().get('openaiOAuth') or {}).removesuffix('/responses') + '/images/' + ('generations' if action=='generate' else 'edits')
        url, headers = apply_codex_workspace_routing(url, headers, account)
        options.pop('input_fidelity', None)  # verified unsupported by gpt-image-2; prompt adapter above
        payload.update(options)
        if parsed.size: payload['size'] = parsed.size
        if parsed.xai_options.get('user'): payload['user'] = parsed.xai_options['user']
        if action == 'edit':
            payload['images'] = [{'image_url': x} for x in parsed.input_images]
            if parsed.mask_url: payload['images'].append({'image_url': parsed.mask_url})
        purpose = 'oauth_openai'
    elif antigravity_images.is_source(source):
        return await antigravity_images.request(
            source, parsed, prompt=_prompt(parsed), n=n, action=action, cfg=cfg,
        )
    elif _is_xai_source(source):
        payload = _xai_image_payload(source, parsed, action=action, n=n)
        if source.key.startswith('oauth:'):
            if channel is None: raise ValueError('image channel is no longer available')
            headers = await channel.build_media_headers()
            url = imagine._media_url(channel, '/images/'+('generations' if action=='generate' else 'edits'))
            purpose = 'oauth_xai'
        else:
            headers = {'Authorization': 'Bearer '+channel.api_key}
            url = _api_url(channel, action); purpose = 'api'
    else:
        if channel is None: raise ValueError('image channel is no longer available')
        headers = {'Authorization': 'Bearer '+channel.api_key}
        url = _api_url(channel, action); purpose = 'api'
        payload.update(options)
        if parsed.native_options.get('style'): payload['style'] = parsed.native_options['style']
        if parsed.xai_options.get('user'): payload['user'] = parsed.xai_options['user']
        if parsed.size: payload['size'] = parsed.size
        payload['response_format'] = 'b64_json'
        # Standard OpenAI edits consume multipart files, even for JSON downstream input.
        if action == 'edit':
            files = parsed._api_edit_files
            if files is None:
                files = await _prepare_api_edit_files(parsed)
    if source.key.startswith('oauth:xai:'):
        return await imagine._request_upstream(channel, method='POST',
            path='/images/'+('generations' if action=='generate' else 'edits'), headers=headers,
            body=json.dumps(payload).encode(), model=source.upstream)
    timeout = httpx.Timeout(float(cfg.get('requestTimeoutSeconds') or 180), connect=15)
    async with network.async_client(timeout=timeout, proxy_purpose=purpose, proxy_channel=source.key,
                                    proxy_model=source.upstream, follow_redirects=False) as client:
        if source.key.startswith('oauth:openai:'):
            image_catalog.current_openai_account(source)
        if source.key.startswith('api:'):
            current = next((row for row in image_catalog.sources() if row.key == source.key and row.model == source.model), None)
            if current is None or not current.available or current.state_key != source.state_key or current.upstream != source.upstream:
                raise ValueError('selected image channel or purpose is no longer available')
        if files is not None:
            return await client.post(url, headers=headers, data={k:str(v) for k,v in payload.items()}, files=files)
        return await client.post(url, headers=headers, json=payload)


async def execute(parsed, *, request, action: str, key_name: str, cfg: dict) -> JSONResponse:
    sources = [s for s in image_catalog.sources() if s.model == parsed.model and s.available and not cooldown.is_blocked(channel_state.effect_key(s), s.upstream)]
    if not sources:
        return JSONResponse({'error': {'message': 'no available image source for this model', 'type':'model_not_available'}}, status_code=503)
    pairs = [(registry.get_channel(s.key), s.upstream) for s in sources]
    pairs = [(channel, model) for channel, model in pairs if channel is not None]
    selection = str(config.get().get('channelSelection') or 'smart').lower()
    if selection == 'smart': pairs = scorer.sort_by_score(pairs)
    elif selection == 'priority': pairs = load_balancing.sort_candidates_by_priority(pairs, config.get(), requested_model=parsed.model)
    order = {channel.key: i for i, (channel, _model) in enumerate(pairs)}
    sources.sort(key=lambda source: order.get(source.key, len(order)))
    if parsed.native_options.get('input_fidelity') not in (None, 'high', 'low'):
        raise ValueError('input_fidelity must be high or low')
    if parsed.native_options.get('background') not in (None, 'auto', 'opaque', 'transparent'):
        raise ValueError('background must be auto, opaque or transparent')
    if parsed.requested_n > MAX_GENERATIONS:
        raise ValueError(f'n exceeds request generation budget ({MAX_GENERATIONS})')
    image_artifacts.dimensions(parsed.size)
    if parsed.native_options.get('partial_images') or getattr(parsed, 'stream', False):
        raise ValueError('stream/partial_images are not supported on the unified final-image endpoint')
    # Validate local constraints before spending a generation.
    fmt = parsed.native_options.get('output_format')
    if fmt and fmt not in ('png', 'jpeg', 'jpg', 'webp'): raise ValueError('output_format must be png, jpeg or webp')
    if parsed.native_options.get('background') == 'transparent' and fmt in ('jpeg', 'jpg'):
        raise ValueError('JPEG cannot represent transparency; use png or webp')
    compression = parsed.native_options.get('output_compression')
    if compression is not None and (not 0 <= compression <= 100 or fmt not in ('jpeg', 'jpg', 'webp')):
        raise ValueError('output_compression requires jpeg/webp and a value of 0..100')
    # Known source-specific constraints are checked before tokens, slots, logs
    # or paid POSTs. Incompatible sources do not consume attempts or suppress a
    # compatible API mapping for the same client-visible model.
    compatible = []; capability_notes = []
    for candidate in sources:
        try:
            if candidate.key.startswith('oauth:openai:') and parsed.native_options.get('moderation') not in (None, 'auto'):
                raise ValueError('Codex Images does not expose non-default moderation; use auto or a compatible API image source')
            if antigravity_images.is_source(candidate):
                antigravity_images.validate_request(parsed, action=action)
            if _is_xai_source(candidate):
                _xai_image_payload(candidate, parsed, action=action, n=parsed.requested_n)
        except ValueError as exc:
            capability_notes.append(f'{candidate.provider} ({candidate.upstream}): {exc}')
        else:
            compatible.append(candidate)
    if not compatible:
        raise ValueError('no image source supports these parameters: ' + '; '.join(dict.fromkeys(capability_notes)))
    sources = compatible
    ag_parsed = None
    try:
        if action == 'edit':
            parsed = copy.copy(parsed)
            # Public capability URLs may point at an HTTP/private deployment. Only
            # exact locally issued URLs are inlined; external URL policy is unchanged.
            parsed.input_images = [await image_artifacts.inline_local_reference(ref) for ref in parsed.input_images]
            if parsed.mask_url:
                parsed.mask_url = await image_artifacts.inline_local_reference(parsed.mask_url)
            ag_sources = [source for source in sources if antigravity_images.is_source(source)]
            if ag_sources:
                # AG consumes Gemini inlineData. Keep its resolved inputs on a
                # source-specific copy so an AG-incompatible URL cannot alter or
                # block xAI/OpenAI/API candidates for the same client model.
                try:
                    ag_parsed = await antigravity_images.prepare_edit_inputs(parsed)
                except (httpx.TransportError, ValueError) as exc:
                    remaining = [source for source in sources if not antigravity_images.is_source(source)]
                    if not remaining:
                        if isinstance(exc, httpx.TimeoutException):
                            return JSONResponse({'error': {
                                'type': 'image_input_timeout',
                                'message': 'reference image or mask download timed out; no generation was attempted',
                            }}, status_code=504)
                        if isinstance(exc, httpx.TransportError):
                            return JSONResponse({'error': {
                                'type': 'image_input_error',
                                'message': 'reference image or mask download failed; no generation was attempted',
                            }}, status_code=502)
                        raise
                    capability_notes.extend(
                        f'{source.provider} ({source.upstream}): reference input is not usable by Antigravity'
                        for source in ag_sources
                    )
                    sources = remaining
            if any(_uses_api_multipart(source) for source in sources):
                parsed._api_edit_files = await _prepare_api_edit_files(parsed)
        mask = None
        mask_inputs = ag_parsed or parsed
        if mask_inputs.mask_url:
            mask = image_artifacts.prepare_mask(
                await image_artifacts.reference_bytes(mask_inputs.input_images[0]),
                await image_artifacts.reference_bytes(mask_inputs.mask_url),
            )
    except httpx.TimeoutException:
        # Input retrieval precedes any paid POST; do not label this an unknown
        # generation outcome or leak the reference URL from the transport error.
        return JSONResponse({'error': {
            'type': 'image_input_timeout',
            'message': 'reference image or mask download timed out; no generation was attempted',
        }}, status_code=504)
    log_task = asyncio.create_task(imagine._start_media_log(request_id=str(uuid.uuid4()), api_key_name=key_name,
        provider=sources[0].provider, media_type='image', action=action, model=parsed.model,
        prompt=parsed.prompt, size=parsed.size, requested_count=parsed.requested_n))
    log_id = None
    cache_task = None
    started = time.monotonic()
    data = []; usages = []; warnings = ['skipped incompatible source: ' + note for note in dict.fromkeys(capability_notes)]; calls = 0; failure = None; status = 200
    if parsed.native_options.get('input_fidelity') and any(s.key.startswith('oauth:') for s in sources):
        warnings.append('input_fidelity adapted as reference-preservation prompt guidance; not a native fidelity control')
    response_headers = {}
    cache_paths = []; media_bytes = 0; actual_qualities = []
    source = sources[0]
    log_source = source  # Last actually attempted source, not a skipped candidate.
    max_calls = parsed.requested_n + len(sources) - 1
    try:
        log_id = await await_owned(log_task)
        while len(data) < parsed.requested_n:
            response = None
            for source in list(sources):
                if calls >= max_calls: raise ValueError('image upstream attempt budget exhausted; not regenerated')
                effect_key = channel_state.effect_key(source)
                if not await concurrency.try_acquire(effect_key): continue
                try:
                    log_source = source
                    n = 1 if (
                        source.key.startswith('oauth:openai:')
                        or antigravity_images.is_source(source)
                    ) else parsed.requested_n-len(data)
                    calls += 1
                    usages.append(None)
                    attempt_parsed = (
                        ag_parsed
                        if antigravity_images.is_source(source) and ag_parsed is not None
                        else parsed
                    )
                    response = await _send(source, attempt_parsed, action=action, n=n, cfg=cfg)
                finally:
                    concurrency.release(effect_key)
                if source.key.startswith('oauth:xai:'):
                    # Preserve Imagine's existing health policy, with the frozen
                    # generation so late results cannot affect a replacement.
                    if 200 <= response.status_code < 300:
                        cooldown.clear_on_success(effect_key, source.upstream)
                    elif response.status_code in imagine._EXPLICIT_SAFE_FAILOVER_STATUSES or response.status_code >= 500:
                        cooldown.record_error(effect_key, source.upstream, f'xAI Imagine HTTP {response.status_code}')
                elif antigravity_images.is_source(source):
                    if 200 <= response.status_code < 300:
                        cooldown.clear_on_success(effect_key, source.upstream)
                    elif response.status_code == 429:
                        # Image quota is not exposed as an account-wide quota
                        # bucket. Cool only this model/source; never disable AG
                        # chat based on an image-only rejection.
                        cooldown.record_error(
                            effect_key, source.upstream,
                            f'Antigravity image HTTP {response.status_code}',
                        )
                if response.status_code in SAFE_REJECTIONS:
                    sources.remove(source)
                    continue
                break
            if response is None: raise ValueError('all image sources are at capacity')
            if response.status_code >= 400:
                status = response.status_code
                retry_after = response.headers.get('retry-after', '')
                if status == 429 and retry_after.isdigit(): response_headers['Retry-After'] = retry_after
                # Do not leak authentication material from arbitrary upstream error text.
                message = f'image upstream rejected request (HTTP {status}); not regenerated'
                try:
                    error = response.json().get('error')
                    detail = error.get('message') if isinstance(error, dict) else None
                    if isinstance(detail, str):
                        secrets = []
                        channel = registry.get_channel(source.key)
                        if channel is not None: secrets.append(getattr(channel, 'api_key', ''))
                        account = oauth_manager.get_account(source.key[6:]) if source.key.startswith('oauth:') else {}
                        for field in (
                            'access_token', 'refresh_token', 'id_token', 'email',
                            'workspace_id', 'chatgpt_account_id', 'project_id', 'projectId',
                        ):
                            secrets.append((account or {}).get(field, ''))
                        for secret in secrets:
                            if secret: detail = detail.replace(str(secret), '[private]')
                        message += ': ' + detail[:500]
                except (ValueError, TypeError, AttributeError): pass
                failure = {'type': 'upstream_error', 'message': message}
                break
            obj = response.json()
            if isinstance(obj.get('usage'), dict): usages[-1] = obj['usage']
            batch = obj.get('data')
            if not isinstance(batch, list) or not batch: raise ValueError('upstream completed without images; not retried')
            if len(batch) != n:
                status = 502
                failure = {'type': 'image_count_mismatch', 'message': f'upstream returned {len(batch)} images for n={n}; not automatically regenerated'}
            for item in batch[:parsed.requested_n-len(data)]:
                raw, meta, notes = await image_artifacts.normalize_item(item, parsed=parsed, mask=mask)
                warnings.extend(notes)
                out = {k:v for k,v in item.items() if k in ('generation_id', 'revised_prompt')}
                out.update(meta)
                retained_path = None
                if cfg.get('cacheEnabled'):
                    try:
                        cache_task = asyncio.create_task(asyncio.to_thread(media_cache.cache_inline_base64,
                            [{'base64':base64.b64encode(raw).decode('ascii'), 'mime':meta['mime_type']}],
                            cfg=cfg, provider=source.provider, media_type='image', action=action))
                        cached = await await_owned(cache_task)
                        cache_paths.extend(cached.paths)
                        retained_path = cached.paths[0] if cached.paths else None
                        cache_task = None
                        if cached.error_class: warnings.append('optional media cache write failed; generation was not repeated')
                    except Exception: warnings.append('optional media cache write failed; generation was not repeated')
                if parsed.response_format == 'url':
                    try:
                        out['url'], out['expires_at'] = image_artifacts.publish(raw, mime=meta['mime_type'], cfg=cfg, request=request, provider=source.provider, action=action, index=len(data), retained_path=retained_path)
                    except (ValueError, OSError):
                        status = 507
                        failure = {'type': 'image_delivery_error', 'message': 'media cache cannot publish this image; generated images were not regenerated'}
                        break
                else:
                    out['b64_json'] = base64.b64encode(raw).decode('ascii')
                media_bytes += len(raw)
                data.append(out)
            quality = obj.get('quality')
            if quality: actual_qualities.append(quality)
            if quality and parsed.native_options.get('quality') and quality != parsed.native_options['quality']:
                warnings.append(f'upstream reported quality={quality}, requested={parsed.native_options["quality"]}')
            if failure: break
    except asyncio.CancelledError:
        if log_task.done() and not log_task.cancelled(): log_id = log_task.result()
        if cache_task is not None and cache_task.done() and not cache_task.cancelled():
            cache_paths.extend(cache_task.result().paths)
        await await_owned(imagine._finish_media_log(log_id, status='cancelled', http_status=499,
            provider=log_source.provider,
            account_key=log_source.key[6:] if log_source.key.startswith('oauth:') else log_source.key,
            duration_ms=int((time.monotonic()-started)*1000), image_count=len(data),
            cached_media_count=len(cache_paths), cache_paths=cache_paths, media_bytes=media_bytes,
            error_type='cancelled', error_message='image request cancelled; no regeneration attempted'))
        raise
    except httpx.TimeoutException:
        status = 504; failure = {'type':'outcome_unknown', 'message':'image POST timed out; it may have been billed and was not retried'}
    except Exception as exc:
        status = 502; failure = {'type':'image_result_error', 'message': str(exc) if isinstance(exc, ValueError) else 'image upstream failed; outcome may be unknown; not retried'}
    if parsed.response_format == 'url':
        downloadable = [item for item in data if image_artifacts.url_available(item['url'])]
        if len(downloadable) != len(data):
            data = downloadable
            status = 507
            failure = {'type': 'image_delivery_error', 'message': 'media cache evicted part of this batch; only downloadable URLs returned; no regeneration attempted'}
    reported_usage = any(isinstance(usage, dict) for usage in usages)
    usage_total = _aggregate_usage(usages)
    if usage_total and any(usage is None for usage in usages):
        warnings.append('usage totals include reported values only; some upstream calls did not report usage')
    result = {'created': int(time.time()), 'model': source.upstream, 'data': data,
        'parrot': {'requested_n': parsed.requested_n, 'completed_n': len(data), 'upstream_calls': calls,
            'generation_budget': parsed.requested_n, 'upstream_attempt_budget': max_calls, 'complete': failure is None, 'warnings': list(dict.fromkeys(warnings))}}
    if usage_total: result['usage'] = usage_total
    if reported_usage: result['parrot']['usage_by_call'] = usages
    if data:
        for field in ('size', 'background', 'output_format'):
            if len({x[field] for x in data}) == 1: result[field] = data[0][field]
    if actual_qualities and len(set(actual_qualities)) == 1: result['quality'] = actual_qualities[0]
    if failure: result['error'] = failure
    cache_paths = [path for path in cache_paths if media_cache.artifact_path_is_safe(path, cfg)]
    await await_owned(imagine._finish_media_log(log_id, status='failed' if failure else 'success',
        provider=log_source.provider,
        duration_ms=int((time.monotonic()-started)*1000), image_count=len(data),
        usage=(usages[0] if len(usages)==1 else {**usage_total, 'parrot_usage_by_call': usages}) if reported_usage else None, http_status=status,
        size=data[0]['size'] if data and len({item['size'] for item in data}) == 1 else None,
        output_sizes=[item['size'] for item in data],
        account_key=log_source.key[6:] if log_source.key.startswith('oauth:') else log_source.key,
        account_email=(oauth_manager.get_account(log_source.key[6:]) or {}).get('email') if log_source.key.startswith('oauth:') else None,
        cached_media_count=len(cache_paths), cache_paths=cache_paths, media_bytes=media_bytes,
        error_type=failure['type'] if failure else None, error_message=failure['message'] if failure else None))
    return JSONResponse(result, status_code=status, headers=response_headers)
