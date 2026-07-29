"""Unit tests for the event primitive."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chainwake.core.primitives.base import Match, NeedsMoreData, NoMatch, Primitive
from chainwake.core.primitives.event import EventPrimitive
from chainwake.providers.base import Event, ObservableValue

pytestmark = pytest.mark.unit

_BASE_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_BASE_BLOCK = 2000


def _event(
    event_type: str = "subnet-registered",
    args: dict[str, object] | None = None,
    *,
    block: int = _BASE_BLOCK,
    extrinsic_hash: str | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        raw_event='{"type": "' + event_type + '"}',
        args=args or {},
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=_BASE_TS,
        extrinsic_hash=extrinsic_hash,
    )


def _obs() -> ObservableValue:
    return ObservableValue(
        path="subnet.19.pool.price",
        value=1.0,
        block=_BASE_BLOCK,
        block_hash=f"0x{_BASE_BLOCK:064x}",
        timestamp=_BASE_TS,
    )


# --- Protocol compliance ---


def test_event_satisfies_primitive_protocol() -> None:
    assert isinstance(EventPrimitive(event_type="subnet-registered"), Primitive)


def test_event_name() -> None:
    assert EventPrimitive(event_type="subnet-registered").name == "event"


# --- Basic matching ---


def test_fires_on_matching_event_type() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    outcome = p.evaluate(_event("subnet-registered"))
    assert isinstance(outcome, Match)


def test_does_not_fire_on_wrong_event_type() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    assert isinstance(p.evaluate(_event("transfer")), NoMatch)


def test_observable_value_returns_no_match() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    assert isinstance(p.evaluate(_obs()), NoMatch)


# --- Args filter ---


def test_fires_when_args_match_filter() -> None:
    p = EventPrimitive(event_type="transfer", args_match={"to_subnet": 19, "tao": 1000})
    outcome = p.evaluate(_event("transfer", {"to_subnet": 19, "tao": 1000, "from": "5Fxxx"}))
    assert isinstance(outcome, Match)


def test_does_not_fire_when_filter_key_missing_from_args() -> None:
    p = EventPrimitive(event_type="transfer", args_match={"to_subnet": 19})
    assert isinstance(p.evaluate(_event("transfer", {"from": "5Fxxx"})), NoMatch)


def test_does_not_fire_when_filter_value_mismatch() -> None:
    p = EventPrimitive(event_type="transfer", args_match={"to_subnet": 19})
    assert isinstance(p.evaluate(_event("transfer", {"to_subnet": 64})), NoMatch)


def test_empty_args_match_matches_any_event_of_type() -> None:
    p = EventPrimitive(event_type="subnet-registered", args_match={})
    outcome = p.evaluate(_event("subnet-registered", {"netuid": 42}))
    assert isinstance(outcome, Match)


def test_no_args_match_defaults_to_empty_filter() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    assert isinstance(p.evaluate(_event("subnet-registered")), Match)


# --- Observed payload shape ---


def test_observed_event_payload_fields() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    ev = _event(
        "subnet-registered",
        {"netuid": 42},
        block=9999,
        extrinsic_hash="0xdeadbeef",
    )
    outcome = p.evaluate(ev)
    assert isinstance(outcome, Match)
    obs = outcome.observed
    assert obs["event_type"] == "subnet-registered"
    assert obs["args"] == {"netuid": 42}
    assert obs["block"] == 9999
    assert obs["block_hash"] == f"0x{9999:064x}"
    assert obs["timestamp"] == _BASE_TS.isoformat()
    assert obs["extrinsic_hash"] == "0xdeadbeef"
    assert "raw_event" in obs


def test_extrinsic_hash_none_in_payload() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    outcome = p.evaluate(_event("subnet-registered"))
    assert isinstance(outcome, Match)
    assert outcome.observed["extrinsic_hash"] is None


# --- Stateless: reset is no-op ---


def test_reset_is_noop() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    p.evaluate(_event("subnet-registered"))
    p.reset()
    assert isinstance(p.evaluate(_event("subnet-registered")), Match)


# --- Decoded args are preserved in JSON-safe form ---


def test_non_scalar_args_preserved_in_payload() -> None:
    p = EventPrimitive(event_type="transfer")
    ev = _event("transfer", {"netuid": 19, "weights": [1, 2, 3], "meta": {"key": "val"}})
    outcome = p.evaluate(ev)
    assert isinstance(outcome, Match)
    args = outcome.observed["args"]
    assert isinstance(args, dict)
    assert args == {
        "netuid": 19,
        "weights": [1, 2, 3],
        "meta": {"key": "val"},
    }


# --- Never returns NeedsMoreData ---


def test_never_returns_needs_more_data() -> None:
    p = EventPrimitive(event_type="subnet-registered")
    for _ in range(5):
        outcome = p.evaluate(_event("transfer"))
        assert not isinstance(outcome, NeedsMoreData)
