"""Provider-neutral generated-media cache primitives.

Provider adapters retain responsibility for authenticating and downloading remote
URLs.  This module only accepts already obtained bytes/base64 data and owns the
shared cache root, bounded decoding, safe extensions, atomic writes, and cleanup.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config

HARD_FILE_LIMIT = 128 * 1024 * 1024
_ALLOWED_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".webm", ".mov", ".m4v",
}
_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
}
_ALLOWED_EXTENSIONS = {suffix.lstrip(".") for suffix in _ALLOWED_SUFFIXES}


@dataclass(frozen=True, slots=True)
class CacheResult:
    paths: tuple[str, ...]
    total_bytes: int
    status: str
    error_class: str | None = None


def cache_root(cfg: dict[str, Any], *, create: bool = True) -> Path:
    raw = str(cfg.get("cachePath") or "images").strip() or "images"
    if os.path.isabs(raw):
        root = Path(raw).resolve()
    else:
        data_root = Path(config.DATA_DIR).resolve()
        root = (data_root / raw).resolve()
        try:
            root.relative_to(data_root)
        except ValueError as exc:
            raise ValueError("cachePath escapes data directory") from exc
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("cachePath is not a safe directory")
    return root


def artifact_path_is_safe(path: str | Path, cfg: dict[str, Any]) -> bool:
    target = Path(path)
    try:
        if target.is_symlink() or not target.is_file():
            return False
        root = cache_root(cfg, create=False)
        target.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def file_limit(cfg: dict[str, Any]) -> int:
    try:
        aggregate = int(cfg.get("cacheMaxBytes") or 0)
    except (TypeError, ValueError):
        aggregate = 0
    return min(aggregate, HARD_FILE_LIMIT) if aggregate > 0 else HARD_FILE_LIMIT


def decode_base64(value: str, *, max_bytes: int) -> bytes:
    encoded = str(value or "").strip()
    if not encoded:
        raise ValueError("generated media is empty")
    if len(encoded) * 3 // 4 > max_bytes:
        raise ValueError("generated media exceeds cache file limit")
    try:
        raw = base64.b64decode(encoded, validate=False)
    except Exception as exc:
        raise ValueError("generated media is not valid base64") from exc
    if not raw or len(raw) > max_bytes:
        raise ValueError("generated media is empty or exceeds cache file limit")
    return raw


def decode_data_url(value: str, *, max_bytes: int) -> tuple[bytes, str]:
    header, sep, encoded = str(value or "").partition(",")
    if not sep or not header.lower().startswith("data:") or ";base64" not in header.lower():
        raise ValueError("unsupported generated media data URL")
    mime = header[5:].split(";", 1)[0].strip().lower()
    return decode_base64(encoded, max_bytes=max_bytes), mime


def extension_for(
    *, media_type: str, mime: str = "", source_url: str = "", preferred: str = "",
) -> str:
    normalized_mime = str(mime or "").split(";", 1)[0].strip().lower()
    if normalized_mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[normalized_mime]
    suffix = Path(urlsplit(str(source_url or "")).path).suffix.lower().lstrip(".")
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix in _ALLOWED_EXTENSIONS:
        return suffix
    # Adapter defaults must not replace a format identified by MIME or URL.
    preferred_ext = str(preferred or "").strip().lower().lstrip(".")
    if preferred_ext == "jpeg":
        preferred_ext = "jpg"
    if preferred_ext in _ALLOWED_EXTENSIONS:
        return preferred_ext
    return "mp4" if media_type == "video" else "png"


def write_bytes(
    raw: bytes,
    *,
    cfg: dict[str, Any],
    provider: str,
    media_type: str,
    action: str,
    extension: str,
    index: int,
) -> str:
    if not raw or len(raw) > file_limit(cfg):
        raise ValueError("generated media is empty or exceeds cache file limit")
    ext = str(extension or "").lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValueError("unsupported generated media extension")
    root = cache_root(cfg)
    day = time.strftime("%Y%m%d", time.localtime())
    out_dir = root / day
    if out_dir.exists() and out_dir.is_symlink():
        raise ValueError("cache day directory must not be a symlink")
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = out_dir.resolve()
    try:
        resolved_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("cache destination escapes cache root") from exc
    safe_provider = "".join(ch for ch in str(provider).lower() if ch.isalnum() or ch == "-") or "media"
    safe_type = "video" if media_type == "video" else "image"
    safe_action = "".join(ch for ch in str(action).lower() if ch.isalnum() or ch == "-") or "generate"
    filename = (
        f"{safe_provider}-{safe_type}-{safe_action}-{int(time.time())}-"
        f"{uuid.uuid4().hex[:10]}-{int(index)}.{ext}"
    )
    target = resolved_dir / filename
    temporary = resolved_dir / f".{filename}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(temporary, "xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return str(target)


def cache_inline_base64(
    items: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    provider: str,
    media_type: str,
    action: str,
) -> CacheResult:
    """Cache inline generated media without changing the caller's response items."""
    if not cfg.get("cacheEnabled"):
        return CacheResult((), 0, "disabled")
    paths: list[str] = []
    total = 0
    failure: str | None = None
    limit = file_limit(cfg)
    for index, item in enumerate(items):
        try:
            raw = decode_base64(str(item.get("base64") or ""), max_bytes=limit)
            mime = str(item.get("mime") or "")
            total += len(raw)
            path = write_bytes(
                raw,
                cfg=cfg,
                provider=provider,
                media_type=media_type,
                action=action,
                extension=extension_for(media_type=media_type, mime=mime),
                index=index,
            )
            paths.append(path)
        except Exception as exc:
            failure = type(exc).__name__
    try:
        cleanup(cache_root(cfg), cfg)
    except Exception as exc:
        failure = failure or type(exc).__name__
    status = "failed" if failure else "cached"
    return CacheResult(tuple(paths), total, status, failure)


def cleanup(root: Path, cfg: dict[str, Any]) -> None:
    resolved_root = root.resolve()
    try:
        retention_days = int(cfg.get("cacheRetentionDays") or 0)
        max_bytes = int(cfg.get("cacheMaxBytes") or 0)
    except Exception:
        retention_days, max_bytes = 0, 0
    files: list[Path] = []
    for path in resolved_root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
            files.append(path)
        except (OSError, ValueError):
            continue
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86400
        kept: list[Path] = []
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                else:
                    kept.append(path)
            except OSError:
                pass
        files = kept
    if max_bytes <= 0:
        return
    stats: list[tuple[float, int, Path]] = []
    total = 0
    for path in files:
        try:
            stat = path.stat()
            total += stat.st_size
            stats.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            pass
    stats.sort(key=lambda item: item[0])
    while total > max_bytes and stats:
        count = max(1, (len(stats) + 4) // 5)
        batch, stats = stats[:count], stats[count:]
        for _, size, path in batch:
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                pass
