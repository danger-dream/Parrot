"""Protocol runtime building blocks.

Phase 1 keeps this package intentionally small: it exposes a registry wrapper
around the legacy per-protocol failover toolkit, without changing runtime
behaviour.  Later phases will grow this into full codecs / matrix / stream
runtime abstractions.
"""

from .types import ProtocolToolkit
from .errors import NormalizedError
from .matrix import ProtocolMatrix, RoutePlan
from .usage import Usage, UsageAccumulator

__all__ = [
    "ProtocolToolkit",
    "Usage",
    "UsageAccumulator",
    "NormalizedError",
    "ProtocolMatrix",
    "RoutePlan",
]
