"""Static hosting of the browser WebUI build (``src/webui``) under ``/ui/``.

Gated by ``management.webUi.enabled`` and evaluated per request, so toggling
the switch from Telegram or the Management API takes effect immediately.

* disabled: ``/ui`` and ``/ui/*`` answer the framework's plain 404 and ``/``
  keeps its previous 404;
* enabled: ``/`` and ``/ui`` redirect (307) to ``/ui/``; ``/ui/assets/*`` are
  content-hashed and cached ``immutable`` for a year; ``index.html`` is
  ``no-cache``; any other missing path falls back to ``index.html`` (SPA);
* enabled but no build present: 404 with a short plain-text explanation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response


DEFAULT_ROOT = Path(__file__).resolve().parent / "webui"
# Every method is routed so that non-GET requests keep the plain 404 these
# paths answered before the WebUI existed (instead of a new 405).
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_READ_METHODS = frozenset({"GET", "HEAD"})
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
INDEX_CACHE_CONTROL = "no-cache"
MISSING_BUILD_TEXT = (
    "Parrot WebUI is enabled, but its build output (src/webui) is not installed.\n"
)


def _not_found() -> Response:
    # Byte-identical to the router's default 404 for unknown paths.
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _inside(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` strictly inside ``root``; None for escapes/dirs."""
    if "\x00" in relative:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


class WebUiHost:
    def __init__(self, *, enabled: Callable[[], bool], root: Path | str = DEFAULT_ROOT) -> None:
        self._enabled = enabled
        self.root = Path(root)

    def enabled(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            return False

    def _root(self) -> Path | None:
        root = self.root.resolve()
        return root if (root / "index.html").is_file() else None

    def _index(self, root: Path) -> Response:
        return FileResponse(
            root / "index.html",
            media_type="text/html",
            headers={"Cache-Control": INDEX_CACHE_CONTROL},
        )

    def serve(self, path: str) -> Response:
        if not self.enabled():
            return _not_found()
        root = self._root()
        if root is None:
            return PlainTextResponse(MISSING_BUILD_TEXT, status_code=404)
        relative = path.lstrip("/")
        if relative in {"", "index.html"}:
            return self._index(root)
        if relative.startswith("assets/"):
            target = _inside(root, relative)
            if target is None:
                return PlainTextResponse("Not Found\n", status_code=404)
            return FileResponse(target, headers={"Cache-Control": ASSET_CACHE_CONTROL})
        target = _inside(root, relative)
        if target is None:
            return self._index(root)
        return FileResponse(target, headers={"Cache-Control": INDEX_CACHE_CONTROL})

    def install(self, app: FastAPI) -> None:
        host = self

        @app.api_route("/", methods=_METHODS, include_in_schema=False)
        async def webui_root(request: Request) -> Response:
            if request.method not in _READ_METHODS or not host.enabled():
                return _not_found()
            return RedirectResponse("/ui/", status_code=307)

        @app.api_route("/ui", methods=_METHODS, include_in_schema=False)
        async def webui_redirect(request: Request) -> Response:
            if request.method not in _READ_METHODS or not host.enabled():
                return _not_found()
            return RedirectResponse("/ui/", status_code=307)

        @app.api_route("/ui/{path:path}", methods=_METHODS, include_in_schema=False)
        async def webui_files(path: str, request: Request) -> Response:
            if request.method not in _READ_METHODS:
                return _not_found()
            return host.serve(path)


def install_webui(app: FastAPI, *, enabled: Callable[[], bool], root: Path | str | None = None) -> WebUiHost:
    host = WebUiHost(enabled=enabled, root=root or DEFAULT_ROOT)
    host.install(app)
    return host
