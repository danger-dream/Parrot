"""Transport-neutral identity and authentication primitives for management."""

from .approvals import (
    ApprovalError,
    ApprovalNotification,
    ApprovalService,
    ApprovalStatus,
    ApprovalView,
    IssuedApproval,
)
from .policy import CapabilityDenied, authorize, has_capability
from .principal import (
    ADMINISTRATOR_CAPABILITIES,
    AuthMethod,
    Capability,
    ManagementPrincipal,
    Role,
)
from .sessions import (
    AuthenticationRateLimited,
    IssuedSession,
    SessionAuthenticationError,
    SessionDevice,
    SessionPolicy,
    SessionService,
    VerifiedSession,
)
from .store import ManagementStateStore

__all__ = [
    "ADMINISTRATOR_CAPABILITIES",
    "ApprovalError",
    "ApprovalNotification",
    "ApprovalService",
    "ApprovalStatus",
    "ApprovalView",
    "AuthMethod",
    "AuthenticationRateLimited",
    "Capability",
    "CapabilityDenied",
    "IssuedApproval",
    "IssuedSession",
    "ManagementPrincipal",
    "ManagementStateStore",
    "Role",
    "SessionAuthenticationError",
    "SessionDevice",
    "SessionPolicy",
    "SessionService",
    "VerifiedSession",
    "authorize",
    "has_capability",
]
