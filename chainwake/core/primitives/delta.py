"""Delta primitive.

Fires when a numeric observable moves by N% from a baseline. Stateful — for
explicit rolling windows it holds a ring of ``(block, timestamp, value)``
entries and compares against the oldest in-window value. With the default
``ever`` window it retains the first successful observation for the lifetime
of the watcher.

Window types (mutually exclusive):
    ``ever``    — first successful observation since watcher start
    ``time``    — wall-clock duration string, e.g. ``"1h"``, ``"30m"``, ``"5d"``
    ``blocks``  — integer number of blocks, e.g. ``"50"``
    ``epochs``  — integer number of Bittensor epochs, e.g. ``"5"``

Operators:
    ``drop-pct``  — fires when pct_change ≤ -target
    ``rise-pct``  — fires when pct_change ≥ +target
    ``move-pct``  — fires when |pct_change| ≥ target

``Match.observed`` conforms to ``ObservedDelta`` in ``output/schema.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from chainwake.core.duration import duration_to_seconds
from chainwake.core.primitives.base import Match, PrimitiveOutcome, needs_more_data, no_match
from chainwake.providers.base import Event, ObservableValue

DeltaOperator = Literal["drop-pct", "rise-pct", "move-pct"]
WindowUnit = Literal["ever", "time", "blocks", "epochs"]

_MIN_TICKS_FOR_DELTA = 2


def _window_to_magnitude(unit: WindowUnit, value: str) -> float:
    """Normalize a window specification to a scalar magnitude.

    Returns seconds for ``"time"`` windows, block count for ``"blocks"``
    windows, and observed epoch count for ``"epochs"`` windows.

    Raises:
        InvalidDurationError: if a ``"time"`` value cannot be parsed.
    """
    if unit == "ever":
        return float("inf")
    if unit == "time":
        return duration_to_seconds(value)
    if unit == "blocks":
        return float(value)
    return float(value)


@dataclass(frozen=True, slots=True)
class _Tick:
    block: int
    timestamp: datetime
    value: float
    epoch_index: int | None


class DeltaPrimitive:
    """Fires when a numeric observable moves by N% from a baseline.

    Args:
        operator: ``"drop-pct"``, ``"rise-pct"``, or ``"move-pct"``.
        target: the percentage magnitude that triggers a match (positive float).
        window_unit: ``"ever"`` or one of the explicit rolling-window units
            ``"time"``, ``"blocks"``, or ``"epochs"``.
        window_value: the window size as a string (e.g. ``"1h"`` for time,
            ``"50"`` for blocks, ``"5"`` for epochs).
    """

    name: str = "delta"

    def __init__(
        self,
        *,
        operator: DeltaOperator,
        target: float,
        window_unit: WindowUnit,
        window_value: str,
    ) -> None:
        if not math.isfinite(target) or target <= 0:
            raise ValueError("delta target must be finite and greater than zero")
        window_magnitude = _window_to_magnitude(window_unit, window_value)
        if window_unit != "ever" and (not math.isfinite(window_magnitude) or window_magnitude <= 0):
            raise ValueError("explicit window must be finite and greater than zero")
        self._operator = operator
        self._target = target
        self._window_unit = window_unit
        self._window_magnitude = window_magnitude
        self._ticks: list[_Tick] = []

    def evaluate(self, observation: ObservableValue | Event) -> PrimitiveOutcome:
        """Evaluate one tick against the configured baseline/window."""
        if not isinstance(observation, ObservableValue):
            return no_match()
        value = observation.value
        if not isinstance(value, int | float):
            return no_match()

        current = _Tick(
            block=observation.block,
            timestamp=observation.timestamp,
            value=float(value),
            epoch_index=_epoch_index(observation),
        )

        if self._window_unit == "epochs" and current.epoch_index is None:
            return needs_more_data("epoch window requires chain epoch state")

        if not self._ticks:
            self._ticks.append(current)
            return needs_more_data("window filling: no prior tick")

        self._ticks.append(current)
        self._prune(current)

        if len(self._ticks) < _MIN_TICKS_FOR_DELTA:
            return needs_more_data("window filling: only one in-window tick after pruning")

        oldest = self._ticks[0]
        return self._check(observation, current, oldest)

    def reset(self) -> None:
        """Clear the sliding window."""
        self._ticks.clear()

    def _prune(self, current: _Tick) -> None:
        """Remove ticks that have fallen outside the window."""
        if self._window_unit == "ever":
            # The first successful observation is the permanent baseline.
            # Keep only it plus the current tick to avoid unbounded memory use.
            self._ticks = [self._ticks[0], current]
        elif self._window_unit == "time":
            cutoff = current.timestamp - timedelta(seconds=self._window_magnitude)
            self._ticks = [t for t in self._ticks if t.timestamp >= cutoff]
        elif self._window_unit == "blocks":
            cutoff_block = current.block - self._window_magnitude
            self._ticks = [t for t in self._ticks if t.block >= cutoff_block]
        else:
            if current.epoch_index is None:
                raise RuntimeError("epoch window requires an epoch index")
            cutoff_epoch = current.epoch_index - self._window_magnitude
            self._ticks = [
                tick
                for tick in self._ticks
                if tick.epoch_index is not None and tick.epoch_index >= cutoff_epoch
            ]

    def _check(
        self,
        observation: ObservableValue,
        current: _Tick,
        oldest: _Tick,
    ) -> PrimitiveOutcome:
        """Compare current against oldest in-window tick."""
        if oldest.value == 0.0:
            return no_match()

        delta = current.value - oldest.value
        delta_pct = (delta / abs(oldest.value)) * 100.0

        if not self._operator_fired(delta_pct):
            return no_match()

        return Match(
            observed={
                "path": observation.path,
                "value": current.value,
                "previous_value": oldest.value,
                "delta": delta,
                "delta_pct": delta_pct,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
                "meta": observation.meta,
            }
        )

    def _operator_fired(self, delta_pct: float) -> bool:
        if self._operator == "drop-pct":
            return delta_pct <= -self._target
        if self._operator == "rise-pct":
            return delta_pct >= self._target
        # move-pct
        return abs(delta_pct) >= self._target


def _epoch_index(observation: ObservableValue) -> int | None:
    value = observation.meta.get("epoch_index")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["DeltaOperator", "DeltaPrimitive", "WindowUnit"]
