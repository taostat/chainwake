"""Primitive contract test pack.

Primitive implementations plug in by importing the
`primitive_contract_tests` parametrised pattern and pointing it at their
`Primitive` factory. Two fake implementations run the pack here to prove the
contract tests are exercisable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from chainwake.core.primitives.base import (
    Match,
    NeedsMoreData,
    NoMatch,
    Primitive,
    PrimitiveInput,
    PrimitiveOutcome,
    drive,
    needs_more_data,
    no_match,
)
from chainwake.providers.base import ObservableValue

pytestmark = pytest.mark.unit


def _ts() -> datetime:
    return datetime(2026, 5, 5, 19, 42, 10, tzinfo=UTC)


def _value(value: float, *, block: int = 8_119_123) -> ObservableValue:
    return ObservableValue(
        path="subnet.19.pool.price",
        value=value,
        block=block,
        block_hash=f"0x{block:x}",
        timestamp=_ts(),
    )


class AlwaysMatchPrimitive:
    name: str = "always-match"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, observation: PrimitiveInput) -> PrimitiveOutcome:
        self.calls += 1
        if not isinstance(observation, ObservableValue):
            return no_match()
        return Match(
            observed={
                "path": observation.path,
                "value": observation.value,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
            }
        )

    def reset(self) -> None:
        self.calls = 0


class ThresholdBelowPrimitive:
    """Realistic stand-in used to exercise the threshold primitive contract."""

    name: str = "threshold"

    def __init__(self, target: float, *, min_observations: int = 0) -> None:
        self._target = target
        self._min_observations = min_observations
        self._observations = 0

    def evaluate(self, observation: PrimitiveInput) -> PrimitiveOutcome:
        if not isinstance(observation, ObservableValue):
            return no_match()
        self._observations += 1
        if self._observations < self._min_observations:
            return needs_more_data(f"have {self._observations}/{self._min_observations}")
        if not isinstance(observation.value, int | float):
            return no_match()
        if observation.value >= self._target:
            return no_match()
        return Match(
            observed={
                "path": observation.path,
                "value": observation.value,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
            }
        )

    def reset(self) -> None:
        self._observations = 0


def test_runtime_checkable_protocol_recognises_impl() -> None:
    assert isinstance(AlwaysMatchPrimitive(), Primitive)
    assert isinstance(ThresholdBelowPrimitive(target=0.05), Primitive)


def test_protocol_rejects_missing_method() -> None:
    class NotAPrimitive:
        name = "x"

    assert not isinstance(NotAPrimitive(), Primitive)


def test_match_is_frozen() -> None:
    match = Match(observed={"x": 1})
    with pytest.raises(FrozenInstanceError):
        Match.__setattr__(match, "observed", {"y": 2})


def test_no_match_is_singleton() -> None:
    assert no_match() is no_match()


def test_needs_more_data_singleton_on_empty_reason() -> None:
    assert needs_more_data() is needs_more_data()


def test_needs_more_data_with_reason_distinct() -> None:
    a = needs_more_data("waiting")
    b = needs_more_data()
    assert a != b
    assert a.reason == "waiting"
    assert b.reason == ""


def test_always_match_returns_match_on_first_observation() -> None:
    primitive = AlwaysMatchPrimitive()
    outcome = primitive.evaluate(_value(0.05))
    assert isinstance(outcome, Match)
    assert outcome.observed["path"] == "subnet.19.pool.price"
    assert outcome.observed["value"] == 0.05


def test_threshold_below_does_not_fire_above() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05)
    assert isinstance(primitive.evaluate(_value(0.06)), NoMatch)


def test_threshold_below_fires_under_target() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05)
    outcome = primitive.evaluate(_value(0.0432))
    assert isinstance(outcome, Match)
    assert outcome.observed["value"] == 0.0432


def test_threshold_below_needs_more_data_until_min_observations() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05, min_observations=3)
    assert isinstance(primitive.evaluate(_value(0.04)), NeedsMoreData)
    assert isinstance(primitive.evaluate(_value(0.04)), NeedsMoreData)
    outcome = primitive.evaluate(_value(0.04))
    assert isinstance(outcome, Match)


def test_reset_clears_state() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05, min_observations=2)
    primitive.evaluate(_value(0.04))
    primitive.reset()
    assert isinstance(primitive.evaluate(_value(0.04)), NeedsMoreData)


async def _stream(values: list[ObservableValue]) -> AsyncIterator[PrimitiveInput]:
    for value in values:
        yield value


async def test_drive_returns_first_match() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05)
    stream = _stream([_value(0.06), _value(0.055), _value(0.04), _value(0.03)])
    match = await drive(primitive, stream)
    assert match is not None
    assert match.observed["value"] == 0.04


async def test_drive_returns_none_when_no_match() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05)
    stream = _stream([_value(0.06), _value(0.07)])
    assert await drive(primitive, stream) is None


async def test_drive_skips_needs_more_data() -> None:
    primitive = ThresholdBelowPrimitive(target=0.05, min_observations=2)
    stream = _stream([_value(0.04), _value(0.04), _value(0.04)])
    match = await drive(primitive, stream)
    assert match is not None
