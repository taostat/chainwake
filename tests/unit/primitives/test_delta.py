"""Unit tests for the delta primitive."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from chainwake.core.primitives.base import Match, NeedsMoreData, NoMatch, Primitive
from chainwake.core.primitives.delta import DeltaPrimitive
from chainwake.providers.base import Event, ObservableValue

pytestmark = pytest.mark.unit

_BASE_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_BASE_BLOCK = 1000


def _obs(
    value: float,
    *,
    block: int = _BASE_BLOCK,
    ts: datetime | None = None,
    path: str = "subnet.19.pool.price",
    epoch_index: int | None = None,
) -> ObservableValue:
    meta: dict[str, object] = {}
    if epoch_index is not None:
        meta["epoch_index"] = epoch_index
    return ObservableValue(
        path=path,
        value=value,
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=ts or _BASE_TS,
        meta=meta,
    )


def _event() -> Event:
    return Event(
        event_type="subnet-registered",
        raw_event="{}",
        args={},
        block=_BASE_BLOCK,
        block_hash="0x" + "0" * 64,
        timestamp=_BASE_TS,
    )


# --- Protocol compliance ---


def test_delta_satisfies_primitive_protocol() -> None:
    assert isinstance(
        DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="time", window_value="1h"),
        Primitive,
    )


def test_delta_name() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="time", window_value="1h")
    assert p.name == "delta"


# --- NeedsMoreData on first tick ---


def test_first_tick_returns_needs_more_data() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="50")
    assert isinstance(p.evaluate(_obs(1.0, block=1000)), NeedsMoreData)


def test_event_returns_no_match() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="50")
    assert isinstance(p.evaluate(_event()), NoMatch)


# --- drop-pct operator ---


def test_drop_pct_fires_on_exact_threshold() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))  # seed
    outcome = p.evaluate(_obs(0.95, block=1050))
    assert isinstance(outcome, Match)
    assert abs(cast(float, outcome.observed["delta_pct"]) + 5.0) < 1e-9


def test_drop_pct_fires_on_larger_drop() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    outcome = p.evaluate(_obs(0.80, block=1050))
    assert isinstance(outcome, Match)
    assert cast(float, outcome.observed["delta_pct"]) < -5.0


def test_drop_pct_does_not_fire_on_smaller_drop() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    assert isinstance(p.evaluate(_obs(0.97, block=1050)), NoMatch)


def test_drop_pct_does_not_fire_on_rise() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    assert isinstance(p.evaluate(_obs(1.10, block=1050)), NoMatch)


# --- rise-pct operator ---


def test_rise_pct_fires_on_threshold() -> None:
    p = DeltaPrimitive(operator="rise-pct", target=10.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    outcome = p.evaluate(_obs(1.10, block=1050))
    assert isinstance(outcome, Match)
    assert abs(cast(float, outcome.observed["delta_pct"]) - 10.0) < 1e-9


def test_rise_pct_does_not_fire_on_drop() -> None:
    p = DeltaPrimitive(operator="rise-pct", target=10.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    assert isinstance(p.evaluate(_obs(0.85, block=1050)), NoMatch)


# --- move-pct operator ---


def test_move_pct_fires_on_drop() -> None:
    p = DeltaPrimitive(operator="move-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    outcome = p.evaluate(_obs(0.94, block=1050))
    assert isinstance(outcome, Match)


def test_move_pct_fires_on_rise() -> None:
    p = DeltaPrimitive(operator="move-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    outcome = p.evaluate(_obs(1.06, block=1050))
    assert isinstance(outcome, Match)


def test_move_pct_does_not_fire_on_small_move() -> None:
    p = DeltaPrimitive(operator="move-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    assert isinstance(p.evaluate(_obs(1.02, block=1050)), NoMatch)


# --- Window pruning: blocks ---


def test_blocks_window_prunes_old_ticks() -> None:
    # window=10 blocks; old tick outside window becomes unavailable
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="10")
    p.evaluate(_obs(1.0, block=1000))
    # block 1011 is 11 blocks ahead; the block=1000 tick should be pruned
    # (cutoff = 1011 - 10 = 1001, so 1000 < 1001 → pruned)
    outcome = p.evaluate(_obs(0.50, block=1011))
    # After pruning, only 1 tick remains → NeedsMoreData
    assert isinstance(outcome, NeedsMoreData)


def test_blocks_window_uses_oldest_in_window() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(2.0, block=900))  # tick 1 — oldest in window
    p.evaluate(_obs(1.9, block=950))  # tick 2
    # current vs oldest: (1.8 - 2.0) / 2.0 = -10% → fires
    outcome = p.evaluate(_obs(1.80, block=1000))
    assert isinstance(outcome, Match)
    assert outcome.observed["previous_value"] == 2.0


# --- Window pruning: time ---


def test_time_window_prunes_old_ticks() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="time", window_value="1m")
    t0 = _BASE_TS
    p.evaluate(_obs(1.0, block=1000, ts=t0))
    # 2 minutes later — old tick should be pruned
    t1 = t0 + timedelta(seconds=121)
    outcome = p.evaluate(_obs(0.50, block=1100, ts=t1))
    assert isinstance(outcome, NeedsMoreData)


def test_time_window_fires_within_window() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="time", window_value="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(1.0, block=1000, ts=t0))
    t1 = t0 + timedelta(minutes=30)
    outcome = p.evaluate(_obs(0.93, block=1100, ts=t1))
    assert isinstance(outcome, Match)


# --- Unbounded window: first successful observation ---


def test_ever_window_keeps_first_observation_as_baseline() -> None:
    p = DeltaPrimitive(
        operator="drop-pct",
        target=5.0,
        window_unit="ever",
        window_value="watcher-start",
    )
    p.evaluate(_obs(2.0, block=1, ts=_BASE_TS))
    p.evaluate(_obs(1.98, block=5_000, ts=_BASE_TS + timedelta(days=365)))
    outcome = p.evaluate(_obs(1.80, block=10_000, ts=_BASE_TS + timedelta(days=730)))

    assert isinstance(outcome, Match)
    assert outcome.observed["previous_value"] == 2.0
    assert outcome.observed["delta_pct"] == pytest.approx(-10.0)


def test_ever_window_ignores_unsuccessful_observations_before_baseline() -> None:
    p = DeltaPrimitive(
        operator="rise-pct",
        target=10.0,
        window_unit="ever",
        window_value="watcher-start",
    )
    assert isinstance(p.evaluate(_event()), NoMatch)
    assert isinstance(p.evaluate(_obs(100.0, block=2)), NeedsMoreData)
    outcome = p.evaluate(_obs(110.0, block=3))

    assert isinstance(outcome, Match)
    assert outcome.observed["previous_value"] == 100.0


# --- Window pruning: epochs ---


def test_epochs_window_prunes_old_ticks() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="epochs", window_value="1")
    p.evaluate(_obs(1.0, block=1_000, epoch_index=7))
    # Two actual subnet epoch transitions put the seed outside a one-epoch window,
    # regardless of the small block distance.
    outcome = p.evaluate(_obs(0.50, block=1_010, epoch_index=9))
    assert isinstance(outcome, NeedsMoreData)


def test_epochs_window_fires_within_window() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="epochs", window_value="1")
    p.evaluate(_obs(1.0, block=1_000, epoch_index=7))
    # One stateful subnet epoch is still in-window even if it spans far more
    # than the legacy global 360-block assumption.
    outcome = p.evaluate(_obs(0.93, block=2_000, epoch_index=8))
    assert isinstance(outcome, Match)


def test_epochs_window_needs_chain_epoch_state() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="epochs", window_value="1")
    assert isinstance(p.evaluate(_obs(1.0, block=1_000)), NeedsMoreData)
    assert isinstance(p.evaluate(_obs(0.5, block=1_010)), NeedsMoreData)


# --- Observed payload shape ---


def test_observed_delta_payload_fields() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    outcome = p.evaluate(_obs(0.90, block=1050))
    assert isinstance(outcome, Match)
    obs = outcome.observed
    assert obs["path"] == "subnet.19.pool.price"
    assert obs["value"] == pytest.approx(0.90)
    assert obs["previous_value"] == 1.0
    assert obs["delta"] == pytest.approx(-0.10)
    assert obs["delta_pct"] == pytest.approx(-10.0)
    assert obs["block"] == 1050
    assert obs["block_hash"] == f"0x{1050:064x}"


# --- Reset clears window ---


def test_reset_clears_sliding_window() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(1.0, block=1000))
    p.reset()
    # After reset, first tick is NeedsMoreData again
    assert isinstance(p.evaluate(_obs(0.50, block=1050)), NeedsMoreData)


# --- Zero-denominator guard ---


def test_zero_previous_value_returns_no_match() -> None:
    p = DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="blocks", window_value="100")
    p.evaluate(_obs(0.0, block=1000))
    assert isinstance(p.evaluate(_obs(0.50, block=1050)), NoMatch)


# --- Duration parsing ---


def test_duration_parse_error() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        DeltaPrimitive(operator="drop-pct", target=5.0, window_unit="time", window_value="1x")
