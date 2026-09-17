"""Stable aggregation point for Management API domain routers."""

from __future__ import annotations

from threading import RLock
from typing import Iterable

from fastapi import APIRouter, FastAPI

from .routers import (
    apikey,
    channels,
    content_blacklist,
    load_balancing,
    logs,
    mapping,
    mcp,
    media,
    media_settings,
    model_metadata,
    models,
    network,
    oauth,
    overview,
    proxy,
    retention,
    search,
    stats,
    status,
    status_alerts,
    system_settings,
    translation,
    updates,
)
from .routers.foundation import router as foundation_router


_PREFIX = "/api/management/v1"
# Ordering is part of routing semantics: static retention routes must precede
# the logs router's dynamic /logs/{logId} routes.
_BUILTIN_DOMAIN_ROUTERS: tuple[APIRouter, ...] = (
    oauth.router,
    channels.router,
    apikey.router,
    overview.router,
    status.router,
    stats.router,
    retention.router,
    logs.router,
    media.router,
    mapping.router,
    model_metadata.router,
    models.router,
    load_balancing.router,
    proxy.router,
    translation.router,
    status_alerts.router,
    updates.router,
    media_settings.router,
    system_settings.router,
    search.router,
    content_blacklist.router,
    network.router,
    mcp.router,
)
_registered: list[APIRouter] = []
_lock = RLock()


def register_management_router(router: APIRouter) -> None:
    """Register one bounded extension router before the application is composed."""
    if not isinstance(router, APIRouter):
        raise TypeError("management domain router must be an APIRouter")
    with _lock:
        if any(existing is router for existing in _registered):
            raise ValueError("management domain router is already registered")
        _registered.append(router)


def _selected_routers(
    extra_routers: Iterable[APIRouter] | None,
) -> tuple[APIRouter, ...]:
    selected = (
        _BUILTIN_DOMAIN_ROUTERS
        if extra_routers is None
        else tuple(extra_routers)
    )
    with _lock:
        registered = tuple(_registered)
    return (foundation_router, *registered, *selected)


def install_management_routers(
    app: FastAPI,
    extra_routers: Iterable[APIRouter] | None = None,
) -> None:
    """Mount each domain router directly so production routes are cloned once."""
    for domain_router in _selected_routers(extra_routers):
        app.include_router(domain_router, prefix=_PREFIX)


def create_management_router(
    extra_routers: Iterable[APIRouter] | None = None,
) -> APIRouter:
    """Compose a standalone aggregate router for bounded test applications."""
    aggregate = APIRouter(prefix=_PREFIX)
    for domain_router in _selected_routers(extra_routers):
        aggregate.include_router(domain_router)
    return aggregate
