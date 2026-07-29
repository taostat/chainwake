"""Unit tests for chainwake.core.errors."""

from __future__ import annotations

import pytest

from chainwake.core.errors import (
    AuthError,
    BudgetExhaustedError,
    ChainwakeError,
    CUExhaustedError,
    DecodeError,
    ProviderError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
    UserError,
)

pytestmark = pytest.mark.unit


class TestHierarchy:
    def test_all_provider_errors_are_chainwake_errors(self) -> None:
        for cls in (
            AuthError,
            RPCUnreachableError,
            RateLimitError,
            SubscriptionFailedError,
            DecodeError,
            CUExhaustedError,
        ):
            err = cls("msg")
            assert isinstance(err, ChainwakeError)
            assert isinstance(err, ProviderError)

    def test_budget_exhausted_is_chainwake_error(self) -> None:
        err = BudgetExhaustedError("spent", "max_ru_reached")
        assert isinstance(err, ChainwakeError)
        assert not isinstance(err, ProviderError)

    def test_provider_error_subclasses_are_catchable_as_provider_error(self) -> None:
        with pytest.raises(ProviderError):
            raise AuthError("bad key")


class TestReasons:
    def test_auth_error_reason(self) -> None:
        assert AuthError.reason == "auth_failed"

    def test_rpc_unreachable_reason(self) -> None:
        assert RPCUnreachableError.reason == "rpc_unreachable"

    def test_rate_limit_reason(self) -> None:
        assert RateLimitError.reason == "rate_limited"

    def test_subscription_failed_reason(self) -> None:
        assert SubscriptionFailedError.reason == "subscription_failed"

    def test_decode_error_reason(self) -> None:
        assert DecodeError.reason == "decode_failed"

    def test_cu_exhausted_has_no_provider_reason(self) -> None:
        # CUExhaustedError is translated by the runtime into a budget-
        # exhausted payload, never into a provider-error payload, so it must
        # not advertise a stale ProviderError-style reason literal.
        err = CUExhaustedError("CUs exhausted")
        assert not hasattr(err, "reason")

    def test_budget_exhausted_max_ru(self) -> None:
        err = BudgetExhaustedError("limit hit", "max_ru_reached")
        assert err.reason == "max_ru_reached"

    def test_budget_exhausted_cu(self) -> None:
        err = BudgetExhaustedError("CU gone", "provider_compute_units_exhausted")
        assert err.reason == "provider_compute_units_exhausted"


class TestMessages:
    def test_message_preserved(self) -> None:
        err = RPCUnreachableError("connection refused to ws://x:9944")
        assert "connection refused" in str(err)

    def test_budget_message_preserved(self) -> None:
        err = BudgetExhaustedError("100 RU limit reached", "max_ru_reached")
        assert "100 RU limit reached" in str(err)


class TestUserError:
    def test_default_reason_is_invalid_input(self) -> None:
        err = UserError("bad path param")
        assert err.reason == "invalid_input"

    def test_explicit_reason_override(self) -> None:
        err = UserError("missing netuid", reason="invalid_path_params")
        assert err.reason == "invalid_path_params"

    def test_message_preserved(self) -> None:
        err = UserError("missing netuid")
        assert str(err) == "missing netuid"

    def test_inherits_from_exception(self) -> None:
        err = UserError("anything")
        assert isinstance(err, Exception)

    def test_not_a_chainwake_error(self) -> None:
        # User errors are not chainwake bugs. A bare ``except ChainwakeError``
        # must not swallow them; the dispatch layer routes them to user_error
        # exit 2 instead of internal_error exit 4.
        err = UserError("not a bug")
        assert not isinstance(err, ChainwakeError)
