"""State primitive.

Fires when the value at a path changes. Retains the previous value across
ticks to detect transitions.

Operators:
    ``on-change``      — fires on any change from the previous value
    ``changes-to``     — fires when value becomes ``target``
    ``changes-from``   — fires when value transitions away from ``target``

First tick: ``NeedsMoreData`` (no prior value to compare against).
Subsequent ticks: compare current to previous; fire if the operator matches.

``Match.observed`` conforms to ``ObservedState`` in ``output/schema.py``.

Collection values:
    When the observable returns a JSON ``dict`` or ``list``, only
    ``on-change`` fires.
    ``Match.observed`` includes a ``changed_keys`` list — the sorted set of
    keys whose values differ between the previous and current dict snapshot.
    List matches include the complete previous and current snapshots.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, cast

from chainwake.core.primitives.base import Match, PrimitiveOutcome, needs_more_data, no_match
from chainwake.providers.base import Event, ObservableValue

StateOperator = Literal["on-change", "changes-to", "changes-from"]
_StateValue = str | int | float | bool | None
_ScalarTypes = (str, int, float, bool, type(None))


class StatePrimitive:
    """Fires when the observable value transitions.

    Args:
        operator: ``"on-change"``, ``"changes-to"``, or ``"changes-from"``.
        target: required for ``"changes-to"`` and ``"changes-from"``;
            ignored (and should be ``None``) for ``"on-change"``.
    """

    name: str = "state"

    def __init__(self, *, operator: StateOperator, target: _StateValue = None) -> None:
        self._operator = operator
        self._target = target
        self._previous: _StateValue | dict[str, object] | list[object] = None
        self._seen = False

    def evaluate(self, observation: ObservableValue | Event) -> PrimitiveOutcome:
        """Return ``Match`` on a qualifying transition, else ``NoMatch``."""
        if not isinstance(observation, ObservableValue):
            return no_match()

        value = observation.value
        if isinstance(value, dict):
            coerced: dict[str, object] = {str(k): v for k, v in value.items()}
            return self._evaluate_dict(observation, coerced)
        if isinstance(value, list):
            return self._evaluate_list(observation, cast(list[object], value))
        if not isinstance(value, _ScalarTypes):
            return no_match()
        return self._evaluate_scalar(observation, value)

    def reset(self) -> None:
        """Clear prior-value state."""
        self._previous = None
        self._seen = False

    def _evaluate_scalar(
        self, observation: ObservableValue, current: _StateValue
    ) -> PrimitiveOutcome:
        if not self._seen:
            self._previous = current
            self._seen = True
            return needs_more_data("no prior value yet")

        previous = self._previous
        scalar_previous: _StateValue = previous if isinstance(previous, _ScalarTypes) else None
        self._previous = current

        if current == scalar_previous:
            return no_match()
        if not self._operator_fired(scalar_previous, current):
            return no_match()

        return Match(
            observed={
                "path": observation.path,
                "value": current,
                "previous_value": scalar_previous,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
            }
        )

    def _evaluate_dict(
        self, observation: ObservableValue, current: dict[str, object]
    ) -> PrimitiveOutcome:
        if not self._seen:
            self._previous = deepcopy(current)
            self._seen = True
            return needs_more_data("no prior value yet")

        previous = self._previous
        self._previous = deepcopy(current)

        if not isinstance(previous, dict) or current == previous:
            return no_match()

        if self._operator != "on-change":
            return no_match()

        changed_keys = sorted(
            k for k in set(current) | set(previous) if current.get(k) != previous.get(k)
        )
        return Match(
            observed={
                "path": observation.path,
                "value": current,
                "previous_value": previous,
                "changed_keys": changed_keys,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
            }
        )

    def _evaluate_list(
        self, observation: ObservableValue, current: list[object]
    ) -> PrimitiveOutcome:
        if not self._seen:
            self._previous = deepcopy(current)
            self._seen = True
            return needs_more_data("no prior value yet")

        previous = self._previous
        self._previous = deepcopy(current)

        if not isinstance(previous, list) or current == previous:
            return no_match()

        if self._operator != "on-change":
            return no_match()

        return Match(
            observed={
                "path": observation.path,
                "value": current,
                "previous_value": previous,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
            }
        )

    def _operator_fired(self, previous: _StateValue, current: _StateValue) -> bool:
        if self._operator == "on-change":
            return True
        if self._operator == "changes-to":
            return current == self._target
        # changes-from
        return previous == self._target


__all__ = ["StateOperator", "StatePrimitive"]
