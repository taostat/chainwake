"""CLI duration helpers — thin re-export from ``chainwake.core.duration``.

All public symbols are imported here so callers that already reference
``chainwake.cli.duration`` continue to work without change.
"""

from __future__ import annotations

from chainwake.core.duration import (
    InvalidDurationError,
    duration_to_seconds,
    parse_duration,
    parse_duration_components,
)

__all__ = [
    "InvalidDurationError",
    "duration_to_seconds",
    "parse_duration",
    "parse_duration_components",
]
