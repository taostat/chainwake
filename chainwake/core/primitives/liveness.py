"""Liveness primitive.

Fires when the time since the last non-stale observation exceeds a
``silent-for`` duration. Stateful — tracks the block and timestamp of the
most recent non-stale tick.

Block-number liveness observables such as ``LastUpdate`` and ``LastTxBlock``
carry an absolute activity marker. Re-reading the same non-zero marker is
silence; a changed marker is activity. Other truthy values are treated as
activity pulses. For event-stream use cases the runtime drives ``Event`` values
through; those are always considered activity.

Duration syntax (``silent_for`` constructor argument):
    ``"30s"``       — 30 seconds
    ``"5m"``        — 5 minutes
    ``"2h"``        — 2 hours
    ``"7d"``        — 7 days
    ``"100blocks"`` — 100 blocks (compared via block number)
    ``"5epochs"``   — 5 observed epochs on the path's subnet

``NeedsMoreData`` until the first observation is received.

``Match.observed`` conforms to ``ObservedLiveness`` in ``output/schema.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from chainwake.core.duration import parse_duration_components
from chainwake.core.primitives.base import Match, PrimitiveOutcome, needs_more_data, no_match
from chainwake.providers.base import Event, ObservableValue

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60

LivenessOperator = Literal["silent-for"]


@dataclass(frozen=True, slots=True)
class _Anchor:
    block: int
    timestamp: datetime | None
    epoch_index: int | None = None


class LivenessPrimitive:
    """Fires when an observable goes silent for longer than the configured duration.

    Args:
        silent_for: duration string; see module docstring for syntax.
    """

    name: str = "liveness"

    def __init__(self, *, silent_for: str) -> None:
        self._unit, self._magnitude = parse_duration_components(silent_for)
        if not math.isfinite(self._magnitude) or self._magnitude <= 0:
            raise ValueError("silent_for must be finite and greater than zero")
        self._anchor: _Anchor | None = None
        self._activity_marker: int | None = None

    def evaluate(self, observation: ObservableValue | Event) -> PrimitiveOutcome:
        """Check if silence threshold has been exceeded."""
        if isinstance(observation, Event):
            return self._evaluate_event(observation)

        if not isinstance(observation, ObservableValue):
            return no_match()

        return self._evaluate_observable(observation)

    def _evaluate_observable(self, observation: ObservableValue) -> PrimitiveOutcome:
        """Evaluate a polled value, including absolute activity markers."""
        epoch_index = _epoch_index(observation)
        context_error: str | None = None
        if self._unit == "epochs" and epoch_index is None:
            context_error = "epoch liveness requires chain epoch state"

        marker = _absolute_activity_marker(observation)
        if context_error is None and marker is not None and marker > 0:
            context_error = _missing_activity_context(observation, self._unit)
        if context_error is not None:
            return needs_more_data(context_error)

        if self._anchor is None:
            self._activity_marker = marker
            self._anchor = _anchor_for(observation, epoch_index, marker)
            # Absolute markers already identify the last activity before the
            # watcher started.  With historical chain context attached by the
            # provider, evaluate immediately instead of starting a new window
            # at watcher launch.
            if marker is not None and marker > 0:
                elapsed, elapsed_str = self._elapsed(observation, epoch_index)
                if elapsed >= self._magnitude:
                    return self._match(observation, elapsed_str)
            return needs_more_data("no prior observation yet")

        # LastUpdate/LastTxBlock values are absolute block markers. Seeing the
        # same non-zero marker again is not fresh activity; only a marker
        # change resets silence. Detect that change before evaluating the
        # threshold so an update exactly on the boundary cannot false-fire.
        if marker is not None and marker != self._activity_marker:
            self._activity_marker = marker
            self._anchor = _anchor_for(observation, epoch_index, marker)
            return no_match()

        is_activity_pulse = marker is None and bool(observation.value)
        elapsed, elapsed_str = self._elapsed(observation, epoch_index)
        if elapsed < self._magnitude:
            if is_activity_pulse:
                self._anchor = _anchor_for(observation, epoch_index)
            return no_match()

        return self._match(observation, elapsed_str)

    def _match(self, observation: ObservableValue, elapsed: str) -> Match:
        """Build a schema-shaped liveness match from the current anchor."""
        if self._anchor is None:
            raise RuntimeError("liveness match requires an activity anchor")
        return Match(
            observed={
                "path": observation.path,
                "last_seen_block": self._anchor.block,
                "last_seen_timestamp": (
                    self._anchor.timestamp.isoformat()
                    if self._anchor.timestamp is not None
                    else None
                ),
                "elapsed": elapsed,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
            }
        )

    def reset(self) -> None:
        """Clear the last-seen anchor."""
        self._anchor = None
        self._activity_marker = None

    def _evaluate_event(self, event: Event) -> PrimitiveOutcome:
        if self._unit == "epochs":
            return needs_more_data("epoch liveness requires chain epoch state")
        self._anchor = _Anchor(block=event.block, timestamp=event.timestamp)
        return no_match()

    def _elapsed(
        self,
        observation: ObservableValue,
        epoch_index: int | None,
    ) -> tuple[float, str]:
        if self._anchor is None:
            raise RuntimeError("liveness elapsed time requires an activity anchor")
        if self._unit == "time":
            if self._anchor.timestamp is None:
                raise RuntimeError("time liveness requires an anchor timestamp")
            elapsed = (observation.timestamp - self._anchor.timestamp).total_seconds()
            return elapsed, _format_elapsed_seconds(elapsed)
        if self._unit == "blocks":
            elapsed = observation.block - self._anchor.block
            return elapsed, f"{elapsed}blocks"
        if epoch_index is None:
            raise RuntimeError("epoch liveness requires a current epoch index")
        if self._anchor.epoch_index is None:
            raise RuntimeError("epoch liveness requires an anchor epoch index")
        elapsed = epoch_index - self._anchor.epoch_index
        return elapsed, f"{elapsed:g}epochs"


def _format_elapsed_seconds(seconds: float) -> str:
    """Human-readable elapsed duration string."""
    total = int(seconds)
    if total < _SECONDS_PER_MINUTE:
        return f"{total}s"
    minutes, secs = divmod(total, _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def _epoch_index(observation: ObservableValue) -> int | None:
    value = observation.meta.get("epoch_index")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _absolute_activity_marker(observation: ObservableValue) -> int | None:
    """Return an on-chain activity block marker, excluding boolean pulses."""
    value = observation.value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _activity_timestamp(observation: ObservableValue) -> datetime | None:
    value = observation.meta.get("activity_timestamp")
    return value if isinstance(value, datetime) else None


def _activity_epoch_index(observation: ObservableValue) -> int | None:
    value = observation.meta.get("activity_epoch_index")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _missing_activity_context(observation: ObservableValue, unit: str) -> str | None:
    """Fail closed when an absolute marker lacks required historical state."""
    if unit == "time" and _activity_timestamp(observation) is None:
        return (
            "time liveness needs the activity block timestamp; "
            "use a historical RPC endpoint or a block duration"
        )
    if unit == "epochs" and _activity_epoch_index(observation) is None:
        return (
            "epoch liveness needs the activity block epoch; "
            "use a historical RPC endpoint or a block duration"
        )
    return None


def _anchor_for(
    observation: ObservableValue,
    epoch_index: int | None,
    activity_marker: int | None = None,
) -> _Anchor:
    historical_timestamp = _activity_timestamp(observation)
    historical_epoch = _activity_epoch_index(observation)
    return _Anchor(
        # Zero means the account/neuron has never been active. In that case
        # start the silence window when the watcher first observes it rather
        # than pretending genesis was a known last-seen point.
        block=(
            activity_marker
            if activity_marker is not None and activity_marker > 0
            else observation.block
        ),
        timestamp=(
            historical_timestamp
            if activity_marker is not None and activity_marker > 0
            else observation.timestamp
        ),
        epoch_index=historical_epoch if activity_marker is not None else epoch_index,
    )


__all__ = ["LivenessOperator", "LivenessPrimitive"]
