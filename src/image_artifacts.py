"""Validate/normalize image bytes and publish short-lived capability URLs.

No crop and no fake transparency: resize uses contain+padding. A mask composites
only its transparent region; opaque pixels come from the original input.
"""
from __future__ import annotations
import asyncio
import base64
import io
import re
import secrets
import time
from pathlib import Path
from PIL import Image, ImageOps
from fastapi import Request
from fastapi.responses import FileResponse, Response
from . import media_cache
from .providers.remote_image import download_https_image

URL_TTL_SECONDS = 3600
MAX_PIXELS = 16_777_216
# token -> (path, mime, expiry, media_type)。media_type 决定下载时的路径归属校验
# （图片与视频分属缓存下不同子树），也决定对外 URL 前缀语义。
_ASSETS: dict[str, tuple[str, str, float, str]] = {}
_IMAGE_URL_PREFIX = '/v1/images/assets'
_MCP_URL_PREFIX = '/v1/mcp/media'


def dimensions(size: str | None) -> tuple[int, int] | None:
    if not size or size == 'auto': return None
    match = re.fullmatch(r'(\d+)x(\d+)', size)
    if not match: raise ValueError('size must be auto or WIDTHxHEIGHT')
    w, h = map(int, match.groups())
    if min(w, h) < 1 or max(w, h) > 8192 or w*h > MAX_PIXELS:
        raise ValueError('size exceeds the image processing limit (8192 per edge, 16MP)')
    return w, h


async def reference_bytes(url: str) -> bytes:
    if url.startswith('data:'):
        return media_cache.decode_data_url(url, max_bytes=media_cache.HARD_FILE_LIMIT)[0]
    return (await download_https_image(url, max_bytes=media_cache.HARD_FILE_LIMIT))[0]


def open_image(raw: bytes) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(raw))
        if im.width*im.height > MAX_PIXELS: raise ValueError('image exceeds 16MP processing limit')
        im.load()
        return ImageOps.exif_transpose(im)
    except Exception as exc:
        raise ValueError('invalid or oversized image bytes') from exc


def multipart_image(raw: bytes, stem: str, *, force_png: bool = False) -> tuple[str, bytes, str]:
    """Validate bytes and label their actual encoding, not a transposed copy's format.

    Keep native PNG/JPEG/WebP references byte-for-byte; masks are encoded as
    PNG for OpenAI-compatible edits. Other decodable inputs also become PNG.
    """
    decoded = open_image(raw)
    with Image.open(io.BytesIO(raw)) as original:
        fmt = original.format
    extension = {'PNG': 'png', 'JPEG': 'jpeg', 'WEBP': 'webp'}.get(fmt)
    if force_png or extension is None:
        encoded = io.BytesIO()
        decoded.save(encoded, format='PNG')
        raw, extension = encoded.getvalue(), 'png'
    return f'{stem}.{extension}', raw, 'image/' + extension


def contain(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    if im.size == size: return im
    scaled = ImageOps.contain(im, size, Image.Resampling.LANCZOS)
    rgba = im.convert('RGBA')
    # Preserve true transparency, otherwise use the source corner background.
    color = (0, 0, 0, 0) if rgba.getchannel('A').getextrema()[0] < 255 else rgba.getpixel((0, 0))
    out = Image.new('RGBA', size, color)
    out.paste(scaled, ((size[0]-scaled.width)//2, (size[1]-scaled.height)//2))
    return out


def prepare_mask(original: bytes, mask: bytes) -> tuple[Image.Image, Image.Image]:
    source = open_image(original).convert('RGBA')
    stencil = open_image(mask)
    if 'A' not in stencil.getbands() or source.size != stencil.size:
        raise ValueError('mask must have an alpha channel and the same dimensions as the first image')
    return source, stencil.getchannel('A')


def normalize(raw: bytes, *, size: str | None, options: dict, mask=None) -> tuple[bytes, dict, list[str]]:
    im = open_image(raw)
    original_size = im.size
    warnings = []
    if mask is not None:
        original, alpha = mask
        generated = contain(im.convert('RGBA'), original.size)
        im = Image.composite(original, generated, alpha)
        warnings.append('mask applied locally: opaque mask pixels preserve the original image')
    background = options.get('background')
    alpha_range = im.convert('RGBA').getchannel('A').getextrema()
    if background == 'transparent' and alpha_range[0] == 255:
        raise ValueError('upstream did not produce a transparent image; no background removal or regeneration was attempted')
    if background == 'opaque' and alpha_range[0] < 255:
        canvas = Image.new('RGBA', im.size, 'white'); canvas.alpha_composite(im.convert('RGBA')); im = canvas.convert('RGB')
    requested = dimensions(size)
    if requested and im.size != requested:
        im = contain(im, requested)
        warnings.append(f'size adapted from {original_size[0]}x{original_size[1]} with contain/padding; no cropping')
    fmt = str(options.get('output_format') or 'png').lower()
    if fmt == 'jpg': fmt = 'jpeg'
    if fmt not in ('png', 'jpeg', 'webp'): raise ValueError('output_format must be png, jpeg or webp')
    if background == 'transparent' and fmt == 'jpeg': raise ValueError('JPEG cannot represent transparency; use png or webp')
    kwargs = {}
    compression = options.get('output_compression')
    if compression is not None:
        if not 0 <= compression <= 100: raise ValueError('output_compression must be 0..100')
        if fmt == 'png': raise ValueError('output_compression requires jpeg or webp')
        kwargs['quality'] = compression
    if fmt == 'jpeg':
        rgba = im.convert('RGBA'); canvas = Image.new('RGBA', im.size, 'white'); canvas.alpha_composite(rgba); im = canvas.convert('RGB')
    encoded = io.BytesIO(); im.save(encoded, format=fmt.upper(), **kwargs)
    alpha_range = im.convert('RGBA').getchannel('A').getextrema()
    return encoded.getvalue(), {'size': f'{im.width}x{im.height}', 'output_format': fmt,
        'background': 'transparent' if alpha_range[0] < 255 else 'opaque', 'mime_type': 'image/'+fmt}, warnings


def _prune() -> None:
    now = time.time()
    for token, entry in list(_ASSETS.items()):
        path, _mime, expiry = entry[0], entry[1], entry[2]
        if expiry <= now:
            _ASSETS.pop(token, None)
            try: Path(path).unlink(missing_ok=True)
            except OSError: pass


# 临时 URL 文件的命名前缀 -> 扩展名。图片与视频共用同一临时回收策略，
# 否则进程重启后残留的视频临时文件会一直占空间。
_TEMPORARY_SUFFIXES = ('png', 'jpg', 'webp', 'mp4', 'webm', 'mov', 'm4v')


def _reap_stale_temporary(root, cutoff: float) -> None:
    for suffix in _TEMPORARY_SUFFIXES:
        for stale in root.rglob(f'url-image-temporary-*.{suffix}'):
            if not stale.is_symlink() and stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)


def publish(raw: bytes, *, mime: str, cfg: dict, request: Request | None = None, provider: str,
            action: str, index: int, media_type: str = 'image', ttl_seconds: int | None = None,
            url_prefix: str | None = None, base_url: str | None = None) -> tuple[str, int]:
    """发布一份短期可访问的媒体资源，返回 (URL, 过期时间戳)。

    URL 由请求本身还原（``request.base_url``）：反向代理保留 Host 时天然得到
    对外地址，不需要任何域名配置。无 ``request`` 时用显式 ``base_url``
    （MCP 的媒体转存路径即用此形式）。``media_type`` 只决定缓存子树与扩展名校验。
    """
    _prune()
    if len(_ASSETS) >= 4096: raise ValueError('temporary media URL capacity exhausted')
    kind = 'video' if media_type == 'video' else 'image'
    root = media_cache.cache_root(cfg)
    # Reap orphaned temporary URL files after a process restart as well. Never
    # remove retained historical media merely because its URL has expired.
    cutoff = time.time() - URL_TTL_SECONDS
    _reap_stale_temporary(root, cutoff)
    media_cache.cleanup(root, cfg)
    path = media_cache.write_bytes(raw, cfg=cfg, provider='url', media_type=kind, action='temporary',
        extension=media_cache.extension_for(media_type=kind, mime=mime), index=index)
    token = secrets.token_urlsafe(32)
    expiry = int(time.time()) + (URL_TTL_SECONDS if ttl_seconds is None else max(1, int(ttl_seconds)))
    _ASSETS[token] = (path, mime, expiry, kind)
    media_cache.cleanup(root, cfg)
    if not Path(path).is_file():
        _ASSETS.pop(token, None)
        raise ValueError('media URL cache capacity exhausted; generated media was not regenerated')
    if base_url is None:
        if request is None:
            raise ValueError('base_url or request is required to publish media')
        base_url = str(request.base_url)
    prefix = url_prefix or (_MCP_URL_PREFIX if kind == 'video' else _IMAGE_URL_PREFIX)
    return str(base_url).rstrip('/') + prefix + '/' + token, expiry


def media_token(url: str) -> str:
    """从已发布的资源 URL 中取出 token（用于日志关联）。"""
    return str(url or "").rsplit('/', 1)[-1]


def url_available(url: str) -> bool:
    entry = _ASSETS.get(url.rsplit('/', 1)[-1])
    return bool(entry and entry[2] > time.time() and Path(entry[0]).is_file())


async def _serve_asset(token: str, *, kind: str | None = None) -> Response:
    _prune()
    entry = _ASSETS.get(token)
    if not entry: return Response(status_code=404)
    path, mime, _expiry, media_type = entry
    # 两类入口各自只能读取自己的资源类别，避免图片端点被用来取视频。
    if kind is not None and media_type != kind: return Response(status_code=404)
    # 安全校验必须按资源自身的类别选择配置：图片与视频分属缓存下不同子树，
    # 用图片配置去校验视频路径会一律判为不安全。
    effective = kind if kind is not None else media_type
    if effective == 'video':
        from . import media_config
        cfg = media_config.settings('video')
        cfg['_media_kind'] = 'video'
    else:
        from .openai.images_simple import settings
        cfg = settings()
    if not media_cache.artifact_path_is_safe(path, cfg): return Response(status_code=404)
    # FileResponse 原生支持 Range（206 + Accept-Ranges: bytes），视频播放器可拖动。
    return FileResponse(path, media_type=mime, headers={'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})


async def download(request: Request, token: str) -> Response:
    """既有图片入口：只服务图片资源。"""
    return await _serve_asset(token, kind='image')


async def download_mcp_media(request: Request, token: str) -> Response:
    """MCP 媒体入口：图片与视频都可，供工具返回的 URL 使用。"""
    return await _serve_asset(token)


async def normalize_item(item: dict, *, parsed, mask=None) -> tuple[bytes, dict, list[str]]:
    if item.get('b64_json'):
        raw = media_cache.decode_base64(item['b64_json'], max_bytes=media_cache.HARD_FILE_LIMIT)
    elif item.get('url'):
        raw = await reference_bytes(item['url'])
    else: raise ValueError('upstream returned an empty image item')
    return await asyncio.to_thread(normalize, raw, size=parsed.size, options=parsed.native_options, mask=mask)
