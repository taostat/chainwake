"""Threshold primitive.

Fires when a numeric observable crosses an absolute target. Stateless — each
tick is evaluated independently; no window or prior-value state is kept.

Operators:
    ``below``  — fires when value < target
    ``above``  — fires when value > target

``Match.observed`` conforms to ``ObservedThreshold`` in ``output/schema.py``.
"""

from __future__ import annotations

import math
from typing import Literal

from chainwake.core.primitives.base import Match, PrimitiveOutcome, no_match
from chainwake.providers.base import Event, ObservableValue

ThresholdOperator = Literal["below", "above"]


class ThresholdPrimitive:
    """Fires when a numeric observable crosses a target.

    Args:
        operator: ``"below"`` or ``"above"``.
        target: the threshold value to compare against.
    """

    name: str = "threshold"

    def __init__(self, *, operator: ThresholdOperator, target: float) -> None:
        if not math.isfinite(target):
            raise ValueError("threshold target must be finite")
        self._operator: ThresholdOperator = operator
        self._target = target

    def evaluate(self, observation: ObservableValue | Event) -> PrimitiveOutcome:
        """Return ``Match`` when the value crosses the threshold, else ``NoMatch``."""
        if not isinstance(observation, ObservableValue):
            return no_match()
        value = observation.value
        if not isinstance(value, int | float):
            return no_match()
        fired = value < self._target if self._operator == "below" else value > self._target
        if not fired:
            return no_match()
        return Match(
            observed={
                "path": observation.path,
                "value": float(value),
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
                "meta": observation.meta,
            }
        )

    def reset(self) -> None:
        """No-op: threshold is stateless."""


__all__ = ["ThresholdOperator", "ThresholdPrimitive"]
