"""Unit tests for the liveness primitive."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chainwake.core.primitives.base import Match, NeedsMoreData, NoMatch, Primitive
from chainwake.core.primitives.liveness import LivenessPrimitive
from chainwake.providers.base import Event, ObservableValue

pytestmark = pytest.mark.unit

_BASE_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_BASE_BLOCK = 1000


def _obs(
    *,
    block: int = _BASE_BLOCK,
    ts: datetime | None = None,
    path: str = "validator.5Fxxx.weights",
    value: object = True,
    epoch_index: int | None = None,
    activity_ts: datetime | None = None,
    activity_epoch_index: int | None = None,
) -> ObservableValue:
    meta: dict[str, object] = {}
    if epoch_index is not None:
        meta["epoch_index"] = epoch_index
    if activity_ts is not None:
        meta["activity_timestamp"] = activity_ts
    if activity_epoch_index is not None:
        meta["activity_epoch_index"] = activity_epoch_index
    return ObservableValue(
        path=path,
        value=value,
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=ts or _BASE_TS,
        meta=meta,
    )


def _event(*, block: int = _BASE_BLOCK, ts: datetime | None = None) -> Event:
    return Event(
        event_type="weight-set",
        raw_event="{}",
        args={},
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=ts or _BASE_TS,
    )


# --- Protocol compliance ---


def test_liveness_satisfies_primitive_protocol() -> None:
    assert isinstance(LivenessPrimitive(silent_for="1h"), Primitive)


def test_liveness_name() -> None:
    assert LivenessPrimitive(silent_for="1h").name == "liveness"


# --- NeedsMoreData until first observation ---


def test_first_observation_returns_needs_more_data() -> None:
    p = LivenessPrimitive(silent_for="1h")
    assert isinstance(p.evaluate(_obs()), NeedsMoreData)


# --- Time-based silence detection ---


def test_time_fires_when_silent_exceeds_duration() -> None:
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))  # anchor
    t1 = t0 + timedelta(hours=1, seconds=1)
    outcome = p.evaluate(_obs(ts=t1, block=_BASE_BLOCK + 300))
    assert isinstance(outcome, Match)


def test_time_does_not_fire_when_within_duration() -> None:
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))
    t1 = t0 + timedelta(minutes=30)
    assert isinstance(p.evaluate(_obs(ts=t1, block=_BASE_BLOCK + 150)), NoMatch)


def test_time_fires_at_exact_boundary() -> None:
    p = LivenessPrimitive(silent_for="30m")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))
    t1 = t0 + timedelta(minutes=30)
    # exactly at boundary — should fire (>= threshold)
    assert isinstance(p.evaluate(_obs(ts=t1, block=_BASE_BLOCK + 150)), Match)


# --- Block-based silence detection ---


def test_blocks_fires_when_silent_exceeds_duration() -> None:
    p = LivenessPrimitive(silent_for="100blocks")
    p.evaluate(_obs(block=1000))  # anchor
    outcome = p.evaluate(_obs(block=1101))
    assert isinstance(outcome, Match)


def test_blocks_does_not_fire_within_window() -> None:
    p = LivenessPrimitive(silent_for="100blocks")
    p.evaluate(_obs(block=1000))
    assert isinstance(p.evaluate(_obs(block=1050)), NoMatch)


def test_blocks_fires_at_exact_boundary() -> None:
    p = LivenessPrimitive(silent_for="100blocks")
    p.evaluate(_obs(block=1000))
    assert isinstance(p.evaluate(_obs(block=1100)), Match)


# --- Epoch-based silence detection ---


def test_epochs_fires_after_one_epoch() -> None:
    p = LivenessPrimitive(silent_for="1epochs")
    p.evaluate(_obs(block=1_000, epoch_index=41))
    # Owner-triggered epochs can advance after far fewer than 360 blocks.
    assert isinstance(p.evaluate(_obs(block=1_010, epoch_index=42)), Match)


def test_epochs_does_not_fire_within_epoch() -> None:
    p = LivenessPrimitive(silent_for="1epochs")
    p.evaluate(_obs(block=1_000, epoch_index=41))
    # A tempo change can stretch/re-anchor the epoch well beyond 360 blocks.
    assert isinstance(p.evaluate(_obs(block=2_000, epoch_index=41)), NoMatch)


def test_epochs_need_chain_epoch_state() -> None:
    p = LivenessPrimitive(silent_for="1epochs")
    assert isinstance(p.evaluate(_obs(block=1_000)), NeedsMoreData)
    assert isinstance(p.evaluate(_obs(block=2_000)), NeedsMoreData)


def test_epochs_accumulate_while_absolute_activity_marker_is_unchanged() -> None:
    """LastUpdate is a block marker, not a truthy activity pulse on every read."""
    p = LivenessPrimitive(silent_for="3epochs")
    first = _obs(
        value=900,
        block=1_000,
        epoch_index=10,
        activity_ts=_BASE_TS,
        activity_epoch_index=10,
    )
    assert isinstance(p.evaluate(first), NeedsMoreData)
    assert isinstance(
        p.evaluate(
            _obs(
                value=900,
                block=1_010,
                epoch_index=11,
                activity_ts=_BASE_TS,
                activity_epoch_index=10,
            )
        ),
        NoMatch,
    )
    assert isinstance(
        p.evaluate(
            _obs(
                value=900,
                block=1_020,
                epoch_index=12,
                activity_ts=_BASE_TS,
                activity_epoch_index=10,
            )
        ),
        NoMatch,
    )
    outcome = p.evaluate(
        _obs(
            value=900,
            block=1_030,
            epoch_index=13,
            activity_ts=_BASE_TS,
            activity_epoch_index=10,
        )
    )
    assert isinstance(outcome, Match)
    assert outcome.observed["last_seen_block"] == 900


def test_activity_marker_change_at_epoch_boundary_resets_before_matching() -> None:
    """A fresh LastUpdate at the threshold boundary is activity, not silence."""
    p = LivenessPrimitive(silent_for="1epochs")
    p.evaluate(
        _obs(
            value=900,
            block=1_000,
            epoch_index=10,
            activity_ts=_BASE_TS,
            activity_epoch_index=10,
        )
    )

    assert isinstance(
        p.evaluate(
            _obs(
                value=1_010,
                block=1_010,
                epoch_index=11,
                activity_ts=_BASE_TS,
                activity_epoch_index=11,
            )
        ),
        NoMatch,
    )
    outcome = p.evaluate(
        _obs(
            value=1_010,
            block=1_020,
            epoch_index=12,
            activity_ts=_BASE_TS,
            activity_epoch_index=11,
        )
    )
    assert isinstance(outcome, Match)
    assert outcome.observed["last_seen_block"] == 1_010


def test_blocks_measure_from_absolute_activity_marker() -> None:
    """A LastUpdate block carries an older anchor than watcher arrival time."""
    p = LivenessPrimitive(silent_for="100blocks")
    p.evaluate(_obs(value=800, block=1_000))
    outcome = p.evaluate(_obs(value=800, block=1_001))
    assert isinstance(outcome, Match)
    assert outcome.observed["elapsed"] == "201blocks"
    assert outcome.observed["last_seen_block"] == 800
    assert outcome.observed["last_seen_timestamp"] is None


def test_historical_marker_can_match_time_liveness_on_first_read() -> None:
    """A validator already stale for an hour must alert when the watcher starts."""
    activity_ts = _BASE_TS - timedelta(hours=2)
    p = LivenessPrimitive(silent_for="1h")

    outcome = p.evaluate(
        _obs(
            value=800,
            block=1_000,
            ts=_BASE_TS,
            activity_ts=activity_ts,
        )
    )

    assert isinstance(outcome, Match)
    assert outcome.observed["last_seen_block"] == 800
    assert outcome.observed["last_seen_timestamp"] == activity_ts.isoformat()
    assert outcome.observed["elapsed"] == "2h"


def test_historical_marker_can_match_epoch_liveness_on_first_read() -> None:
    """Epoch silence is measured from the marker's historical epoch."""
    p = LivenessPrimitive(silent_for="3epochs")

    outcome = p.evaluate(
        _obs(
            value=800,
            block=1_000,
            epoch_index=20,
            activity_ts=_BASE_TS - timedelta(hours=2),
            activity_epoch_index=16,
        )
    )

    assert isinstance(outcome, Match)
    assert outcome.observed["elapsed"] == "4epochs"


# --- Observed payload shape ---


def test_observed_liveness_payload_fields() -> None:
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(block=1000, ts=t0, path="validator.5Fxxx.weights"))
    t1 = t0 + timedelta(hours=2)
    outcome = p.evaluate(_obs(block=1600, ts=t1, path="validator.5Fxxx.weights"))
    assert isinstance(outcome, Match)
    obs = outcome.observed
    assert obs["path"] == "validator.5Fxxx.weights"
    assert obs["last_seen_block"] == 1000
    assert obs["last_seen_timestamp"] == t0.isoformat()
    assert obs["block"] == 1600
    assert obs["block_hash"] == f"0x{1600:064x}"
    assert obs["timestamp"] == t1.isoformat()
    assert "elapsed" in obs


# --- Active observations advance the anchor ---


def test_active_obs_advances_anchor_preventing_false_positive() -> None:
    """Repeated truthy observations must not accumulate silence from T0."""
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0, block=1000))  # seeds anchor at t0
    t1 = t0 + timedelta(minutes=30)
    # active obs at T+30m — should advance anchor, not fire
    assert isinstance(p.evaluate(_obs(ts=t1, block=1150)), NoMatch)
    t2 = t1 + timedelta(minutes=40)
    # 40m since last active obs (T+30m) — within 1h, no false positive
    assert isinstance(p.evaluate(_obs(ts=t2, block=1350)), NoMatch)


def test_stale_obs_does_not_advance_anchor() -> None:
    """Falsy observations do not count as activity; silence accumulates."""
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0, block=1000, value=True))  # seeds anchor at t0
    t1 = t0 + timedelta(minutes=30)
    # stale (falsy) obs at T+30m — anchor stays at t0
    assert isinstance(p.evaluate(_obs(ts=t1, block=1150, value=False)), NoMatch)
    t2 = t0 + timedelta(hours=1, seconds=1)
    # 1h+1s since anchor (t0), stale obs doesn't reset it → fires
    assert isinstance(p.evaluate(_obs(ts=t2, block=1400, value=False)), Match)


# --- Events count as activity ---


def test_event_resets_anchor() -> None:
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))  # anchor at t0
    t1 = t0 + timedelta(minutes=30)
    # event 30 min later — resets anchor
    p.evaluate(_event(ts=t1, block=_BASE_BLOCK + 150))
    # now 45 min after the event (75 min total) — still within 1h
    t2 = t1 + timedelta(minutes=45)
    assert isinstance(p.evaluate(_obs(ts=t2, block=_BASE_BLOCK + 375)), NoMatch)


def test_event_before_first_obs_seeds_anchor() -> None:
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_event(ts=t0, block=1000))
    # event seeded the anchor; 2h later should fire
    t1 = t0 + timedelta(hours=2)
    assert isinstance(p.evaluate(_obs(ts=t1, block=1600)), Match)


# --- Reset clears anchor ---


def test_reset_clears_anchor() -> None:
    p = LivenessPrimitive(silent_for="1h")
    p.evaluate(_obs())  # seeds anchor
    p.reset()
    assert isinstance(p.evaluate(_obs()), NeedsMoreData)


# --- Duration parse errors ---


def test_bad_duration_raises_value_error() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        LivenessPrimitive(silent_for="1x")


# --- Elapsed string formatting ---


def test_elapsed_string_seconds() -> None:
    p = LivenessPrimitive(silent_for="30s")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))
    t1 = t0 + timedelta(seconds=45)
    outcome = p.evaluate(_obs(ts=t1, block=_BASE_BLOCK + 4))
    assert isinstance(outcome, Match)
    assert outcome.observed["elapsed"] == "45s"


def test_elapsed_string_minutes() -> None:
    p = LivenessPrimitive(silent_for="5m")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))
    t1 = t0 + timedelta(minutes=7)
    outcome = p.evaluate(_obs(ts=t1, block=_BASE_BLOCK + 35))
    assert isinstance(outcome, Match)
    assert outcome.observed["elapsed"] == "7m"


def test_elapsed_string_hours() -> None:
    p = LivenessPrimitive(silent_for="1h")
    t0 = _BASE_TS
    p.evaluate(_obs(ts=t0))
    t1 = t0 + timedelta(hours=3)
    outcome = p.evaluate(_obs(ts=t1, block=_BASE_BLOCK + 900))
    assert isinstance(outcome, Match)
    assert outcome.observed["elapsed"] == "3h"
