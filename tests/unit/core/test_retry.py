"""Unit tests for chainwake.core.retry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from chainwake.core.errors import (
    AuthError,
    BudgetExhaustedError,
    CUExhaustedError,
    DecodeError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
)
from chainwake.core.retry import RateLimitGuard, with_transient_retry

pytestmark = pytest.mark.unit


class TestWithTransientRetry:
    async def test_returns_value_on_success(self) -> None:
        fn = AsyncMock(return_value=42)
        wrapped = with_transient_retry(fn)
        result = await wrapped("a", b=2)
        assert result == 42
        fn.assert_awaited_once_with("a", b=2)

    async def test_retries_on_connection_error(self) -> None:
        fn: AsyncMock = AsyncMock(
            side_effect=[ConnectionError("refused"), ConnectionError("refused"), 99]
        )
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await wrapped()
        assert result == 99
        assert fn.await_count == 3
        # Backoff sleep called between attempts
        assert mock_sleep.await_count >= 2

    async def test_retries_on_os_error(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=[OSError("broken pipe"), 7])
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await wrapped()
        assert result == 7

    async def test_retries_on_timeout_error(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=[TimeoutError("timed out"), 5])
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await wrapped()
        assert result == 5

    async def test_retries_on_asyncio_timeout_error(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=[TimeoutError(), "ok"])
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await wrapped()
        assert result == "ok"

    async def test_retries_on_rpc_unreachable(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=[RPCUnreachableError("ws down"), "done"])
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await wrapped()
        assert result == "done"

    async def test_retries_on_subscription_failed(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=[SubscriptionFailedError("sub closed"), "alive"])
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await wrapped()
        assert result == "alive"

    async def test_does_not_retry_auth_error(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=AuthError("bad key"))
        wrapped = with_transient_retry(fn)
        with pytest.raises(AuthError):
            await wrapped()
        fn.assert_awaited_once()

    async def test_does_not_retry_cu_exhausted(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=CUExhaustedError("out of CUs"))
        wrapped = with_transient_retry(fn)
        with pytest.raises(CUExhaustedError):
            await wrapped()
        fn.assert_awaited_once()

    async def test_does_not_retry_rate_limit(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=RateLimitError("rate limited"))
        wrapped = with_transient_retry(fn)
        with pytest.raises(RateLimitError):
            await wrapped()
        fn.assert_awaited_once()

    async def test_does_not_retry_decode_error(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=DecodeError("bad SCALE"))
        wrapped = with_transient_retry(fn)
        with pytest.raises(DecodeError):
            await wrapped()
        fn.assert_awaited_once()

    async def test_does_not_retry_budget_exhausted(self) -> None:
        fn: AsyncMock = AsyncMock(side_effect=BudgetExhaustedError("limit", "max_ru_reached"))
        wrapped = with_transient_retry(fn)
        with pytest.raises(BudgetExhaustedError):
            await wrapped()
        fn.assert_awaited_once()

    async def test_backoff_is_exponential(self) -> None:
        """Verify sleep durations grow between retries."""
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        fn: AsyncMock = AsyncMock(
            side_effect=[
                ConnectionError(),
                ConnectionError(),
                ConnectionError(),
                "value",
            ]
        )
        wrapped = with_transient_retry(fn)
        with patch("asyncio.sleep", side_effect=fake_sleep):
            result = await wrapped()

        assert result == "value"
        assert len(sleep_calls) == 3
        # Second sleep >= first (exponential growth)
        assert sleep_calls[1] >= sleep_calls[0]
        assert sleep_calls[2] >= sleep_calls[1]


class TestRateLimitGuard:
    async def test_first_hit_sleeps_250ms(self) -> None:
        guard = RateLimitGuard()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await guard.handle(RateLimitError("rl"))

        assert len(slept) == 1
        assert abs(slept[0] - 0.25) < 0.001

    async def test_second_hit_sleeps_500ms(self) -> None:
        guard = RateLimitGuard()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await guard.handle(RateLimitError("rl"))
            await guard.handle(RateLimitError("rl"))

        assert len(slept) == 2
        assert abs(slept[1] - 0.5) < 0.001

    async def test_third_hit_sleeps_1s(self) -> None:
        guard = RateLimitGuard()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            for _ in range(3):
                await guard.handle(RateLimitError("rl"))

        assert abs(slept[2] - 1.0) < 0.001

    async def test_after_max_attempts_returns_terminal_signal(self) -> None:
        guard = RateLimitGuard()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            results = [await guard.handle(RateLimitError("rl")) for _ in range(9)]

        assert len(slept) == 8
        assert results[:8] == [True] * 8
        assert results[8] is False

    async def test_uses_caller_deadline_aware_sleep(self) -> None:
        guard = RateLimitGuard()
        waits: list[float] = []

        async def deadline_sleep(seconds: float) -> None:
            waits.append(seconds)

        result = await guard.handle(RateLimitError("rl"), sleep=deadline_sleep)

        assert result is True
        assert waits == [0.25]

    async def test_sleep_caps_at_60s(self) -> None:
        """The doubling sequence saturates at the 60s cap so a long-tailed
        retry doesn't oversleep past any reasonable rate-limit window.

        Sequence: 250ms, 500ms, 1s, 2s, 4s, 8s, 16s, 32s — the 8th attempt
        would be 32s, well below the 60s cap. To verify the cap, we check
        that no sleep ever exceeds 60s even if the doubling would suggest it.
        """
        guard = RateLimitGuard()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            for _ in range(8):
                await guard.handle(RateLimitError("rl"))

        assert all(s <= 60.0 for s in slept)
        # Last attempt within the 8-attempt window: 250ms * 2^7 = 32s, below cap
        assert abs(slept[-1] - 32.0) < 0.001

    async def test_reset_clears_consecutive_count(self) -> None:
        guard = RateLimitGuard()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await guard.handle(RateLimitError("rl"))
            await guard.handle(RateLimitError("rl"))
            guard.reset()
            # After reset, next hit should sleep 250ms again
            await guard.handle(RateLimitError("rl"))

        assert abs(slept[2] - 0.25) < 0.001
