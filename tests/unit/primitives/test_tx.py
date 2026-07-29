"""Unit tests for the tx primitive."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

import pytest

from chainwake.core.primitives.base import Match, NeedsMoreData, NoMatch, Primitive
from chainwake.core.primitives.tx import TxPrimitive
from chainwake.providers.base import Event, ObservableValue, TxFinalityLevel, TxFinalityStatus

pytestmark = pytest.mark.unit

_BASE_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_BASE_BLOCK = 3000
_TX_HASH = "0x" + "ab" * 32


def _obs_status(
    level: str,
    *,
    tx_hash: str = _TX_HASH,
    block: int = _BASE_BLOCK,
    include_block: bool = True,
    confirmations: int | None = None,
    execution_status: Literal["success", "reverted"] | None = None,
) -> ObservableValue:
    status = TxFinalityStatus(
        tx_hash=tx_hash,
        level=cast(TxFinalityLevel, level),
        block=block if include_block else None,
        block_hash=f"0x{block:064x}" if include_block else None,
        timestamp=_BASE_TS if include_block else None,
        confirmations=confirmations,
        execution_status=execution_status,
        gas_used=21_000 if execution_status is not None else None,
        effective_gas_price_wei=1_500_000_000 if execution_status is not None else None,
    )
    return ObservableValue(
        path=f"tx.{tx_hash}",
        value=status,
        block=block,
        block_hash=f"0x{block:064x}",
        timestamp=_BASE_TS,
    )


def _obs_non_status() -> ObservableValue:
    return ObservableValue(
        path="tx.0xabc",
        value="not-a-status",
        block=_BASE_BLOCK,
        block_hash=f"0x{_BASE_BLOCK:064x}",
        timestamp=_BASE_TS,
    )


def _event() -> Event:
    return Event(
        event_type="transfer",
        raw_event="{}",
        args={},
        block=_BASE_BLOCK,
        block_hash=f"0x{_BASE_BLOCK:064x}",
        timestamp=_BASE_TS,
    )


# --- Protocol compliance ---


def test_tx_satisfies_primitive_protocol() -> None:
    assert isinstance(TxPrimitive(tx_hash=_TX_HASH, finality="included"), Primitive)


def test_tx_name() -> None:
    assert TxPrimitive(tx_hash=_TX_HASH, finality="included").name == "tx"


# --- included finality target ---


def test_fires_when_tx_included() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    outcome = p.evaluate(_obs_status("included"))
    assert isinstance(outcome, Match)


def test_fires_when_tx_finalized_and_target_is_included() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    outcome = p.evaluate(_obs_status("finalized"))
    assert isinstance(outcome, Match)


def test_needs_more_data_when_pending_and_target_included() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    assert isinstance(p.evaluate(_obs_status("pending")), NeedsMoreData)


# --- finalized finality target ---


def test_fires_when_tx_finalized() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="finalized")
    outcome = p.evaluate(_obs_status("finalized"))
    assert isinstance(outcome, Match)


def test_needs_more_data_when_included_and_target_finalized() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="finalized")
    assert isinstance(p.evaluate(_obs_status("included")), NeedsMoreData)


def test_needs_more_data_when_pending_and_target_finalized() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="finalized")
    assert isinstance(p.evaluate(_obs_status("pending")), NeedsMoreData)


# --- Dropped tx ---


def test_dropped_tx_returns_no_match() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    assert isinstance(p.evaluate(_obs_status("dropped")), NoMatch)


# --- Wrong tx hash ---


def test_wrong_tx_hash_returns_no_match() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    assert isinstance(
        p.evaluate(_obs_status("included", tx_hash="0x" + "cd" * 32)),
        NoMatch,
    )


# --- Non-status inputs ---


def test_non_status_value_returns_needs_more_data() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    assert isinstance(p.evaluate(_obs_non_status()), NeedsMoreData)


def test_event_returns_no_match() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    assert isinstance(p.evaluate(_event()), NoMatch)


# --- Terminal: only fires once ---


def test_fires_only_once() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    first = p.evaluate(_obs_status("included"))
    assert isinstance(first, Match)
    second = p.evaluate(_obs_status("included"))
    assert isinstance(second, NoMatch)


# --- Observed payload shape ---


def test_observed_tx_payload_fields() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="finalized")
    outcome = p.evaluate(_obs_status("finalized", block=5000))
    assert isinstance(outcome, Match)
    obs = outcome.observed
    assert obs["tx_hash"] == _TX_HASH
    assert obs["finality"] == "finalized"
    assert obs["block"] == 5000
    assert obs["block_hash"] == f"0x{5000:064x}"
    assert obs["timestamp"] == _BASE_TS.isoformat()


# --- Metadata guard: block/block_hash/timestamp must be present ---


def test_needs_more_data_when_finality_reached_but_no_metadata() -> None:
    """Finality level reached but no block metadata → keep waiting."""
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    outcome = p.evaluate(_obs_status("included", include_block=False))
    assert isinstance(outcome, NeedsMoreData)


def test_fires_once_metadata_becomes_available() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    assert isinstance(p.evaluate(_obs_status("included", include_block=False)), NeedsMoreData)
    assert isinstance(p.evaluate(_obs_status("included", include_block=True)), Match)


# --- Reset clears fired flag ---


def test_reset_allows_refiring() -> None:
    p = TxPrimitive(tx_hash=_TX_HASH, finality="included")
    p.evaluate(_obs_status("included"))
    p.reset()
    assert isinstance(p.evaluate(_obs_status("included")), Match)


def test_confirmation_target_waits_for_requested_depth() -> None:
    primitive = TxPrimitive(tx_hash=_TX_HASH, finality="included", confirmations=3)

    assert isinstance(
        primitive.evaluate(_obs_status("included", confirmations=2, execution_status="success")),
        NeedsMoreData,
    )
    outcome = primitive.evaluate(
        _obs_status("included", confirmations=3, execution_status="success")
    )

    assert isinstance(outcome, Match)
    assert outcome.observed["confirmations"] == 3
    assert outcome.observed["execution_status"] == "success"
    assert outcome.observed["gas_used"] == 21_000
    assert outcome.observed["effective_gas_price_wei"] == 1_500_000_000


def test_confirmation_target_preserves_reverted_execution_context() -> None:
    primitive = TxPrimitive(tx_hash=_TX_HASH, finality="included", confirmations=1)

    outcome = primitive.evaluate(
        _obs_status("included", confirmations=1, execution_status="reverted")
    )

    assert isinstance(outcome, Match)
    assert outcome.observed["execution_status"] == "reverted"


def test_confirmation_target_requires_execution_context() -> None:
    primitive = TxPrimitive(tx_hash=_TX_HASH, finality="included", confirmations=1)

    outcome = primitive.evaluate(_obs_status("included", confirmations=1))

    assert isinstance(outcome, NeedsMoreData)
