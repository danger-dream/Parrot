"""OAuth Management Control public surface."""

from .account_mutations import OAuthReplaceRequired
from .backend import OAuthBackend
from .control import OAuthControl
from .models import (
    CchMode,
    CompleteOAuthLoginCommand,
    CreateOAuthAccountCommand,
    JsonCredential,
    ManualCredential,
    OAuthAccountFilter,
    OAuthAccountSort,
    OAuthCredentialKind,
    OAuthImportDecision,
    OAuthProvider,
    OAuthUsageDisplayMode,
    PageSpec,
    RefreshTokenCredential,
    UpdateOAuthAccountCommand,
)


_DEFAULT_CONTROL = OAuthControl()


def get_oauth_control() -> OAuthControl:
    return _DEFAULT_CONTROL


__all__ = [
    "CchMode",
    "CompleteOAuthLoginCommand",
    "CreateOAuthAccountCommand",
    "JsonCredential",
    "ManualCredential",
    "OAuthAccountFilter",
    "OAuthAccountSort",
    "OAuthBackend",
    "OAuthControl",
    "OAuthCredentialKind",
    "OAuthImportDecision",
    "OAuthProvider",
    "OAuthReplaceRequired",
    "OAuthUsageDisplayMode",
    "PageSpec",
    "RefreshTokenCredential",
    "UpdateOAuthAccountCommand",
    "get_oauth_control",
]
