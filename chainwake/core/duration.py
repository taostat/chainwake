"""Canonical duration parsing for chainwake.

A duration is a single ``<n><unit>`` token where ``<n>`` may be a decimal:

    30s   5m   2h   7d          — wall-clock time
    100blocks   5epochs          — chain-native units (Bittensor)

Accepted by all flags that take a duration (``--silent-for``, ``--max-runtime``,
window sizes in ``--window``, etc.).
"""

from __future__ import annotations

import re
from typing import Literal

_DURATION_RE = re.compile(
    r"^(?P<n>\d+(?:\.\d+)?)(?P<unit>s|m|h|d|blocks?|epochs?)$",
    re.IGNORECASE,
)

_SECONDS_PER_UNIT: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}

_CHAIN_NATIVE_UNITS: frozenset[str] = frozenset({"blocks", "epochs"})


class InvalidDurationError(ValueError):
    """Raised when a duration string cannot be parsed."""


def parse_duration(raw: str) -> str:
    """Validate and normalise a duration string.

    Accepts decimals (e.g. ``"1.5h"``) and chain-native units. Returns the
    canonical form: lower-cased unit, chain-native units always pluralised.

    Args:
        raw: duration string to validate.

    Returns:
        Canonical lower-cased duration string.

    Raises:
        InvalidDurationError: if the string does not match the expected pattern.
    """
    match = _DURATION_RE.match(raw.strip())
    if not match:
        raise InvalidDurationError(
            f"invalid duration {raw!r} — expected e.g. '30s', '5m', '2h', '100blocks', '3epochs'"
        )
    n = match.group("n")
    raw_unit = match.group("unit").lower()
    if raw_unit == "block":
        return f"{n}blocks"
    if raw_unit == "epoch":
        return f"{n}epochs"
    return f"{n}{raw_unit}"


def duration_to_seconds(raw: str) -> float:
    """Convert a wall-clock duration string to fractional seconds.

    Accepts decimals (e.g. ``"1.5h"``). Raises for chain-native units since
    those cannot be converted to seconds without chain state.

    Args:
        raw: duration string.

    Returns:
        Duration in seconds as a float.

    Raises:
        InvalidDurationError: for invalid strings or chain-native units.
    """
    canonical = parse_duration(raw)
    # Check chain-native first to avoid "s" suffix false-matching "blocks".
    for chain_unit in _CHAIN_NATIVE_UNITS:
        if canonical.endswith(chain_unit):
            raise InvalidDurationError(
                f"duration {raw!r} uses chain-native units — cannot convert to seconds"
            )
    for unit, secs in _SECONDS_PER_UNIT.items():
        if canonical.endswith(unit):
            n = float(canonical[: -len(unit)])
            return n * secs
    raise InvalidDurationError(  # pragma: no cover — parse_duration already validates
        f"duration {raw!r} uses chain-native units — cannot convert to seconds"
    )


def parse_duration_components(raw: str) -> tuple[Literal["time", "blocks", "epochs"], float]:
    """Parse a duration into a ``(kind, magnitude)`` pair.

    Used by primitives that need a uniform representation:
    - ``"time"`` → magnitude in seconds
    - ``"blocks"`` → magnitude in block count
    - ``"epochs"`` → magnitude in observed subnet epoch transitions

    Args:
        raw: duration string, including chain-native units.

    Returns:
        A ``(kind, magnitude)`` tuple.

    Raises:
        InvalidDurationError: if the string does not match a known pattern.
    """
    canonical = parse_duration(raw)
    if canonical.endswith("blocks"):
        return "blocks", float(canonical[:-6])
    if canonical.endswith("epochs"):
        return "epochs", float(canonical[:-6])
    return "time", duration_to_seconds(raw)


__all__ = [
    "InvalidDurationError",
    "duration_to_seconds",
    "parse_duration",
    "parse_duration_components",
]
