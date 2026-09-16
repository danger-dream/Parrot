"""Public DTOs and controls for the unified model center.

The model package is imported by older specialist controls for ``common`` helpers,
so public model-center symbols are loaded lazily to avoid a mapping/control cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .common import DomainControl, ListPage

_MODEL_CENTER_EXPORTS = {
    "ModelCenterControl",
    "ModelKind",
    "ModelSourceType",
    "ModelStatus",
    "ModelSourceRef",
    "ModelOwnerRef",
    "ModelIdentity",
    "ModelFilters",
    "ModelSourceView",
    "ModelView",
    "ModelPage",
    "ModelSelectionMode",
    "ModelSelection",
    "ModelStateField",
    "ModelStateTarget",
    "ModelStateItemResult",
    "ModelStateResult",
}

if TYPE_CHECKING:
    from .control import (
        ModelCenterControl,
        ModelFilters,
        ModelIdentity,
        ModelKind,
        ModelOwnerRef,
        ModelPage,
        ModelSelection,
        ModelSelectionMode,
        ModelSourceRef,
        ModelSourceType,
        ModelSourceView,
        ModelStateField,
        ModelStateItemResult,
        ModelStateResult,
        ModelStateTarget,
        ModelStatus,
        ModelView,
    )


def __getattr__(name: str) -> Any:
    if name in _MODEL_CENTER_EXPORTS:
        from . import control
        return getattr(control, name)
    raise AttributeError(name)


__all__ = ["DomainControl", "ListPage", *_MODEL_CENTER_EXPORTS]
