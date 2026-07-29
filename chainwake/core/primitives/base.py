"""Primitive protocol and outcome types.

A primitive consumes an async stream of `ObservableValue` (or `Event` for the
event primitive) and yields one of `Match | NoMatch | NeedsMoreData` per tick.
The runtime drives the stream and surfaces a `Match` to the output adapter.
Threshold, delta, event, liveness, state, and transaction primitives all
implement this protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chainwake.providers.base import Event, ObservableValue


@dataclass(frozen=True, slots=True)
class Match:
    """Primitive fired. Carries the observed-payload fragment.

    `observed` is a JSON-serialisable dict that maps to one of the
    `Observed*` Pydantic models in `chainwake/output/schema.py`. The runtime
    builds the full envelope around it. Carrying a dict (rather than the
    Pydantic model directly) keeps primitives ignorant of the output module —
    they declare what they observed, not how it serialises.
    """

    observed: dict[str, object]


@dataclass(frozen=True, slots=True)
class NoMatch:
    """Primitive evaluated this tick and did not fire. Continue."""


@dataclass(frozen=True, slots=True)
class NeedsMoreData:
    """Primitive cannot decide yet (e.g. delta window still filling).

    Optional `reason` is propagated to stderr at debug level.
    """

    reason: str = ""


PrimitiveOutcome = Match | NoMatch | NeedsMoreData


_NO_MATCH = NoMatch()
_NEEDS_MORE_DATA = NeedsMoreData()


def no_match() -> NoMatch:
    return _NO_MATCH


def needs_more_data(reason: str = "") -> NeedsMoreData:
    if not reason:
        return _NEEDS_MORE_DATA
    return NeedsMoreData(reason=reason)


PrimitiveInput = ObservableValue | Event


@runtime_checkable
class Primitive[T_co](Protocol):
    """Stateful or stateless evaluator of an observable stream.

    Lifecycle:
        - `evaluate(input)` is called once per stream item.
        - Implementations may hold internal state across calls (delta windows,
          liveness anchors). `reset()` clears that state.
        - Returning `Match` ends the watcher; the runtime ignores subsequent
          stream items.
    """

    name: str
    """Primitive name as used in the registry's applicable_primitives."""

    def evaluate(self, observation: T_co) -> PrimitiveOutcome:
        """Evaluate one observation; return outcome for this tick."""
        ...

    def reset(self) -> None:
        """Clear any internal state (delta window, liveness anchor, etc.)."""
        ...


async def drive(
    primitive: Primitive[PrimitiveInput],
    stream: AsyncIterator[PrimitiveInput],
) -> Match | None:
    """Helper: drive a primitive through a stream until first match.

    Returns the `Match` or `None` if the stream exhausts without firing. Used
    by the runtime and by contract tests.
    """

    async for item in stream:
        outcome = primitive.evaluate(item)
        if isinstance(outcome, Match):
            return outcome
    return None


__all__ = [
    "Match",
    "NeedsMoreData",
    "NoMatch",
    "Primitive",
    "PrimitiveInput",
    "PrimitiveOutcome",
    "drive",
    "needs_more_data",
    "no_match",
]
