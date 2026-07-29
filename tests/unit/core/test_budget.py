"""Unit tests for chainwake.core.budget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from chainwake.core.budget import Budget
from chainwake.core.errors import BudgetExhaustedError

pytestmark = pytest.mark.unit


class TestBudgetCounters:
    def test_starts_at_zero(self) -> None:
        b = Budget()
        assert b.rpc_calls == 0
        assert b.estimated_ru_consumed == 0

    def test_charge_rpc_call_increments(self) -> None:
        b = Budget()
        b.charge_rpc_call()
        b.charge_rpc_call()
        assert b.rpc_calls == 2

    def test_estimated_ru_equals_rpc_calls(self) -> None:
        b = Budget()
        b.charge_rpc_call()
        b.charge_rpc_call()
        assert b.estimated_ru_consumed == b.rpc_calls == 2

    def test_multi_read_provider_call_tracks_ru_separately(self) -> None:
        b = Budget()
        b.charge_rpc_call(ru_cost=4)
        assert b.rpc_calls == 1
        assert b.estimated_ru_consumed == 4

    def test_runtime_ms_increases(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(seconds=3.5)
        with patch("chainwake.core.budget.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t1]
            b = Budget()
            ms = b.runtime_ms
        assert ms == 3500

    def test_started_at_is_recorded(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        with patch("chainwake.core.budget.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            b = Budget()
        assert b.started_at == t0


class TestMaxRU:
    def test_no_limit_never_raises(self) -> None:
        b = Budget(max_ru=None)
        for _ in range(1000):
            b.charge_rpc_call()  # should not raise

    def test_allows_charge_at_exact_limit(self) -> None:
        b = Budget(max_ru=3)
        b.charge_rpc_call()
        b.charge_rpc_call()
        b.charge_rpc_call()
        assert b.rpc_calls == 3
        assert b.estimated_ru_consumed == 3

    def test_exact_limit_blocks_only_the_next_call(self) -> None:
        b = Budget(max_ru=1)
        b.ensure_ru_available()
        b.charge_rpc_call()
        with pytest.raises(BudgetExhaustedError):
            b.ensure_ru_available()

    def test_preflight_preserves_exact_cap_counters(self) -> None:
        b = Budget(max_ru=2)
        b.charge_rpc_call()
        b.charge_rpc_call()
        with pytest.raises(BudgetExhaustedError):
            b.ensure_ru_available()
        assert b.rpc_calls == 2
        assert b.estimated_ru_consumed == 2

    def test_preflight_rejects_multi_read_call_before_charging(self) -> None:
        b = Budget(max_ru=3)
        with pytest.raises(BudgetExhaustedError):
            b.ensure_ru_available(4)
        assert b.rpc_calls == 0
        assert b.estimated_ru_consumed == 0

    def test_reservation_blocks_concurrent_preflight_without_charging(self) -> None:
        b = Budget(max_ru=5)
        b.reserve_ru(3)

        with pytest.raises(BudgetExhaustedError):
            b.ensure_ru_available(3)

        assert b.rpc_calls == 0
        assert b.estimated_ru_consumed == 0

    def test_reserved_call_commits_once(self) -> None:
        b = Budget(max_ru=3)
        b.reserve_ru(3)
        b.charge_reserved_rpc_call(ru_cost=3)

        assert b.rpc_calls == 1
        assert b.estimated_ru_consumed == 3

    def test_failed_read_can_release_its_reservation(self) -> None:
        b = Budget(max_ru=3)
        b.reserve_ru(3)
        b.release_ru_reservation(3)
        b.ensure_ru_available(3)


class TestMaxRuntime:
    def test_no_limit_is_runtime_exceeded_false(self) -> None:
        b = Budget(max_runtime_seconds=None)
        assert not b.is_runtime_exceeded()

    def test_is_runtime_exceeded_true_when_past(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t_expired = t0 + timedelta(seconds=31)
        with patch("chainwake.core.budget.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t_expired]
            b = Budget(max_runtime_seconds=30.0)
            assert b.is_runtime_exceeded()

    def test_is_runtime_exceeded_false_before(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t_soon = t0 + timedelta(seconds=1)
        with patch("chainwake.core.budget.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t_soon]
            b = Budget(max_runtime_seconds=30.0)
            assert not b.is_runtime_exceeded()
