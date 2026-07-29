"""Unit tests for the threshold primitive."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chainwake.core.primitives.base import Match, NeedsMoreData, NoMatch, Primitive
from chainwake.core.primitives.threshold import ThresholdPrimitive
from chainwake.providers.base import Event, ObservableValue

pytestmark = pytest.mark.unit


def _ts() -> datetime:
    return datetime(2026, 5, 5, 19, 42, 10, tzinfo=UTC)


def _obs(value: float, *, path: str = "subnet.19.pool.price", block: int = 100) -> ObservableValue:
    return ObservableValue(
        path=path,
        value=value,
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=_ts(),
    )


def _event() -> Event:
    return Event(
        event_type="subnet-registered",
        raw_event="{}",
        args={},
        block=100,
        block_hash="0x" + "0" * 64,
        timestamp=_ts(),
    )


# --- Protocol compliance ---


def test_threshold_satisfies_primitive_protocol() -> None:
    assert isinstance(ThresholdPrimitive(operator="below", target=1.0), Primitive)


def test_threshold_name() -> None:
    assert ThresholdPrimitive(operator="below", target=1.0).name == "threshold"


# --- Below operator ---


def test_below_fires_when_value_strictly_less() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    outcome = p.evaluate(_obs(0.5))
    assert isinstance(outcome, Match)
    assert outcome.observed["value"] == 0.5


def test_below_does_not_fire_at_exactly_target() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    assert isinstance(p.evaluate(_obs(1.0)), NoMatch)


def test_below_does_not_fire_above_target() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    assert isinstance(p.evaluate(_obs(1.5)), NoMatch)


# --- Above operator ---


def test_above_fires_when_value_strictly_greater() -> None:
    p = ThresholdPrimitive(operator="above", target=1.0)
    outcome = p.evaluate(_obs(2.0))
    assert isinstance(outcome, Match)
    assert outcome.observed["value"] == 2.0


def test_above_does_not_fire_at_exactly_target() -> None:
    p = ThresholdPrimitive(operator="above", target=1.0)
    assert isinstance(p.evaluate(_obs(1.0)), NoMatch)


def test_above_does_not_fire_below_target() -> None:
    p = ThresholdPrimitive(operator="above", target=1.0)
    assert isinstance(p.evaluate(_obs(0.5)), NoMatch)


# --- Observed payload shape ---


def test_observed_payload_has_required_fields() -> None:
    p = ThresholdPrimitive(operator="below", target=2.0)
    outcome = p.evaluate(_obs(1.0, path="subnet.1.pool.price", block=42))
    assert isinstance(outcome, Match)
    obs = outcome.observed
    assert obs["path"] == "subnet.1.pool.price"
    assert obs["value"] == 1.0
    assert obs["block"] == 42
    assert obs["block_hash"] == f"0x{42:064x}"
    assert obs["timestamp"] == _ts().isoformat()


def test_observed_value_is_float() -> None:
    p = ThresholdPrimitive(operator="below", target=10.0)
    outcome = p.evaluate(_obs(5))  # int value
    assert isinstance(outcome, Match)
    assert isinstance(outcome.observed["value"], float)


def test_observed_payload_preserves_provider_context() -> None:
    observation = ObservableValue(
        path="token.DAI.price",
        value=1.0001,
        block=42,
        block_hash=f"0x{42:064x}",
        timestamp=_ts(),
        meta={
            "source": "coingecko",
            "quote_currency": "usd",
            "token_address": "0x6b175474e89094c44da98b954eedeac495271d0f",
        },
    )

    outcome = ThresholdPrimitive(operator="above", target=1.0).evaluate(observation)

    assert isinstance(outcome, Match)
    assert outcome.observed["meta"] == observation.meta


# --- Non-numeric and non-ObservableValue inputs ---


def test_non_numeric_value_returns_no_match() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    obs = ObservableValue(
        path="subnet.1.state",
        value="some-string",
        block=1,
        block_hash="0x01",
        timestamp=_ts(),
    )
    assert isinstance(p.evaluate(obs), NoMatch)


def test_event_input_returns_no_match() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    assert isinstance(p.evaluate(_event()), NoMatch)


# --- Stateless: reset is no-op ---


def test_reset_is_noop_for_stateless_primitive() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    p.evaluate(_obs(0.5))  # fires
    p.reset()
    # still fires on next tick
    assert isinstance(p.evaluate(_obs(0.5)), Match)


# --- Never returns NeedsMoreData ---


def test_threshold_never_returns_needs_more_data() -> None:
    p = ThresholdPrimitive(operator="below", target=1.0)
    for _ in range(10):
        outcome = p.evaluate(_obs(2.0))
        assert not isinstance(outcome, NeedsMoreData)
