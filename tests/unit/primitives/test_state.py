"""Unit tests for the state primitive."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chainwake.core.primitives.base import Match, NeedsMoreData, NoMatch, Primitive
from chainwake.core.primitives.state import StatePrimitive
from chainwake.providers.base import Event, ObservableValue

pytestmark = pytest.mark.unit

_BASE_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_BASE_BLOCK = 500


def _obs(
    value: object,
    *,
    path: str = "validator.5Fxxx.commission",
    block: int = _BASE_BLOCK,
) -> ObservableValue:
    return ObservableValue(
        path=path,
        value=value,
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=_BASE_TS,
    )


def _event() -> Event:
    return Event(
        event_type="hyperparameter-set",
        raw_event="{}",
        args={},
        block=_BASE_BLOCK,
        block_hash="0x" + "0" * 64,
        timestamp=_BASE_TS,
    )


# --- Protocol compliance ---


def test_state_satisfies_primitive_protocol() -> None:
    assert isinstance(StatePrimitive(operator="on-change"), Primitive)


def test_state_name() -> None:
    assert StatePrimitive(operator="on-change").name == "state"


# --- NeedsMoreData on first tick ---


def test_first_tick_returns_needs_more_data() -> None:
    p = StatePrimitive(operator="on-change")
    assert isinstance(p.evaluate(_obs("active")), NeedsMoreData)


def test_event_returns_no_match() -> None:
    p = StatePrimitive(operator="on-change")
    assert isinstance(p.evaluate(_event()), NoMatch)


# --- on-change operator ---


def test_on_change_fires_on_any_change() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs("active"))  # seed
    outcome = p.evaluate(_obs("inactive"))
    assert isinstance(outcome, Match)
    assert outcome.observed["value"] == "inactive"
    assert outcome.observed["previous_value"] == "active"


def test_on_change_does_not_fire_when_same() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs("active"))
    assert isinstance(p.evaluate(_obs("active")), NoMatch)


def test_on_change_fires_for_numeric_change() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs(10))
    assert isinstance(p.evaluate(_obs(11)), Match)


def test_on_change_fires_for_bool_change() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs(True))
    assert isinstance(p.evaluate(_obs(False)), Match)


def test_on_change_fires_for_none_to_value() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs(None))
    assert isinstance(p.evaluate(_obs("something")), Match)


# --- changes-to operator ---


def test_changes_to_fires_when_value_becomes_target() -> None:
    p = StatePrimitive(operator="changes-to", target="inactive")
    p.evaluate(_obs("active"))
    outcome = p.evaluate(_obs("inactive"))
    assert isinstance(outcome, Match)


def test_changes_to_does_not_fire_for_other_change() -> None:
    p = StatePrimitive(operator="changes-to", target="inactive")
    p.evaluate(_obs("active"))
    assert isinstance(p.evaluate(_obs("paused")), NoMatch)


def test_changes_to_does_not_fire_when_same_as_target() -> None:
    p = StatePrimitive(operator="changes-to", target="active")
    p.evaluate(_obs("active"))
    assert isinstance(p.evaluate(_obs("active")), NoMatch)


def test_changes_to_fires_for_int_target() -> None:
    p = StatePrimitive(operator="changes-to", target=100)
    p.evaluate(_obs(50))
    assert isinstance(p.evaluate(_obs(100)), Match)


# --- changes-from operator ---


def test_changes_from_fires_when_leaving_target() -> None:
    p = StatePrimitive(operator="changes-from", target="active")
    p.evaluate(_obs("active"))
    outcome = p.evaluate(_obs("inactive"))
    assert isinstance(outcome, Match)
    assert outcome.observed["previous_value"] == "active"


def test_changes_from_does_not_fire_when_not_leaving_target() -> None:
    p = StatePrimitive(operator="changes-from", target="active")
    p.evaluate(_obs("inactive"))
    assert isinstance(p.evaluate(_obs("paused")), NoMatch)


def test_changes_from_does_not_fire_on_no_change() -> None:
    p = StatePrimitive(operator="changes-from", target="active")
    p.evaluate(_obs("active"))
    assert isinstance(p.evaluate(_obs("active")), NoMatch)


# --- Observed payload shape ---


def test_observed_state_payload_fields() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs("alpha", path="network.runtime-version", block=10))
    outcome = p.evaluate(_obs("beta", path="network.runtime-version", block=11))
    assert isinstance(outcome, Match)
    obs = outcome.observed
    assert obs["path"] == "network.runtime-version"
    assert obs["value"] == "beta"
    assert obs["previous_value"] == "alpha"
    assert obs["block"] == 11
    assert obs["block_hash"] == f"0x{11:064x}"
    assert obs["timestamp"] == _BASE_TS.isoformat()


# --- Reset clears prior-value state ---


def test_reset_clears_previous_value() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs("active"))
    p.reset()
    assert isinstance(p.evaluate(_obs("inactive")), NeedsMoreData)


# --- Previous value tracks last seen ---


def test_previous_value_advances_on_each_tick() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs("a"))  # seed
    p.evaluate(_obs("b"))  # fires; previous becomes "b"
    outcome = p.evaluate(_obs("c"))
    assert isinstance(outcome, Match)
    assert outcome.observed["previous_value"] == "b"
    assert outcome.observed["value"] == "c"


# --- List values (child-keys-style JSON state) ---


def test_list_first_tick_returns_needs_more_data() -> None:
    p = StatePrimitive(operator="on-change")
    result = p.evaluate(_obs(["a", "b"]))
    assert isinstance(result, NeedsMoreData)


def test_list_no_change_returns_no_match() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs([{"netuid": 1, "child": "5Child"}]))
    assert isinstance(
        p.evaluate(_obs([{"netuid": 1, "child": "5Child"}])),
        NoMatch,
    )


def test_list_on_change_fires_with_previous_and_current_values() -> None:
    p = StatePrimitive(operator="on-change")
    previous = [{"netuid": 1, "child": "5Child"}]
    current = [
        {"netuid": 1, "child": "5Child"},
        {"netuid": 2, "child": "5Other"},
    ]
    p.evaluate(_obs(previous))

    outcome = p.evaluate(_obs(current))

    assert isinstance(outcome, Match)
    assert outcome.observed["value"] == current
    assert outcome.observed["previous_value"] == previous


def test_list_changes_to_operator_returns_no_match() -> None:
    p = StatePrimitive(operator="changes-to", target="x")
    p.evaluate(_obs(["a"]))
    assert isinstance(p.evaluate(_obs(["b"])), NoMatch)


# --- Dict values (hyperparams-style) ---


def test_dict_first_tick_returns_needs_more_data() -> None:
    p = StatePrimitive(operator="on-change")
    result = p.evaluate(_obs({"tempo": 99}))
    assert isinstance(result, NeedsMoreData)


def test_dict_no_change_returns_no_match() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs({"tempo": 99}))
    assert isinstance(p.evaluate(_obs({"tempo": 99})), NoMatch)


def test_dict_on_change_fires_and_includes_changed_keys() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs({"tempo": 99, "kappa": 32000}))
    outcome = p.evaluate(_obs({"tempo": 100, "kappa": 32000}))
    assert isinstance(outcome, Match)
    assert outcome.observed["changed_keys"] == ["tempo"]
    assert outcome.observed["value"] == {"tempo": 100, "kappa": 32000}
    assert outcome.observed["previous_value"] == {"tempo": 99, "kappa": 32000}


def test_dict_changed_keys_includes_added_and_removed() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs({"tempo": 99}))
    outcome = p.evaluate(_obs({"tempo": 99, "kappa": 32000}))
    assert isinstance(outcome, Match)
    assert outcome.observed["changed_keys"] == ["kappa"]


def test_dict_changes_to_operator_returns_no_match() -> None:
    p = StatePrimitive(operator="changes-to", target="x")
    p.evaluate(_obs({"tempo": 99}))
    assert isinstance(p.evaluate(_obs({"tempo": 100})), NoMatch)


def test_dict_reset_clears_previous() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs({"tempo": 99}))
    p.reset()
    assert isinstance(p.evaluate(_obs({"tempo": 100})), NeedsMoreData)


def test_dict_changed_keys_sorted() -> None:
    p = StatePrimitive(operator="on-change")
    p.evaluate(_obs({"z": 1, "a": 2, "m": 3}))
    outcome = p.evaluate(_obs({"z": 9, "a": 9, "m": 3}))
    assert isinstance(outcome, Match)
    assert outcome.observed["changed_keys"] == ["a", "z"]
