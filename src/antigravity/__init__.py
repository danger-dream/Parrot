"""Antigravity provider-specific runtime helpers."""

from ..openai.transform.guard import GuardError


class DispatchGuardError(GuardError):
    """A prepared AG route became inapplicable before physical dispatch."""

    def __init__(self, status: int, err_type: str, message: str,
                 *, param: str | None = None, scope: str = "candidate",
                 code: str | None = "antigravity_model_unavailable"):
        super().__init__(status, err_type, message, param=param, scope=scope, code=code)
