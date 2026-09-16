"""Shared auxiliary-domain controls used by Telegram and Management API."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from ..operations import OperationRegistry, OperationStore
from .media import (
    AntigravityMediaControl,
    AntigravityMediaSettings,
    ImageControl,
    MediaModelMutationResult,
    XaiMediaControl,
)
from .status_alerts import StatusAlertControl
from .translation import TranslationControl
from .updates import UpdateControl


_binding_lock = RLock()


@dataclass(frozen=True, slots=True)
class AuxiliaryControls:
    translation: TranslationControl
    status_alerts: StatusAlertControl
    updates: UpdateControl
    images: ImageControl
    xai_media: XaiMediaControl
    antigravity_media: AntigravityMediaControl

    def bind_operations(self, store: OperationStore, registry: OperationRegistry) -> None:
        with _binding_lock:
            self.translation.bind_operations(store, registry)
            self.status_alerts.bind_operations(store, registry)
            self.updates.bind_operations(store, registry)


_lock = RLock()
_controls: AuxiliaryControls | None = None


def get_auxiliary_controls() -> AuxiliaryControls:
    global _controls
    with _lock:
        if _controls is None:
            _controls = AuxiliaryControls(
                translation=TranslationControl(),
                status_alerts=StatusAlertControl(),
                updates=UpdateControl(),
                images=ImageControl(),
                xai_media=XaiMediaControl(),
                antigravity_media=AntigravityMediaControl(),
            )
        return _controls


__all__ = [
    "AntigravityMediaControl",
    "AntigravityMediaSettings",
    "AuxiliaryControls",
    "ImageControl",
    "MediaModelMutationResult",
    "StatusAlertControl",
    "TranslationControl",
    "UpdateControl",
    "XaiMediaControl",
    "get_auxiliary_controls",
]
