"""Antigravity provider-specific runtime helpers."""

from ..openai.transform.guard import GuardError


class DispatchGuardError(GuardError):
    """A prepared AG route became inapplicable before physical dispatch."""
