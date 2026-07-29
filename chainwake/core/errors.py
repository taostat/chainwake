"""Exception hierarchy for chainwake.

Maps to the JSON envelope's error status/reason fields:
  - UserError → status: user_error (exit code 2)
  - ProviderError subclasses → status: provider_error
  - BudgetExhaustedError → status: budget_exhausted (exit code 1)
  - ChainwakeError base → status: internal_error (exit code 4)

RPC error codes per spec §9.4:
  - -32021: auth failure (AuthError)
  - -32029: rate limit (RateLimitError)
  - -32030: compute-unit exhaustion (CUExhaustedError)
"""

from __future__ import annotations

from typing import Literal


class ChainwakeError(Exception):
    """Base class for all chainwake-originated errors."""


class UserError(Exception):
    """Raised when CLI input is invalid or refers to a non-existent observable.

    Translated to user_error exit 2 by the dispatch layer. Carry a clear,
    user-actionable message and a `reason` literal that maps to
    UserErrorPayload.reason.

    Distinct from ChainwakeError: user errors are not bugs. A bare
    ``except ChainwakeError`` in retry/budget paths must not swallow them.
    """

    def __init__(self, message: str, *, reason: str = "invalid_input") -> None:
        super().__init__(message)
        self.reason = reason


class ProviderError(ChainwakeError):
    """An error originating from the RPC provider.

    Subclasses carry a `reason` literal that maps 1:1 to the
    `ProviderErrorPayload.reason` field in the output schema.
    """

    reason: Literal[
        "auth_failed",
        "rpc_unreachable",
        "rate_limited",
        "subscription_failed",
        "decode_failed",
    ]


class AuthError(ProviderError):
    """RPC error -32021: invalid API key. Terminal — no retry."""

    reason: Literal["auth_failed"] = "auth_failed"  # type: ignore[assignment]


class RPCUnreachableError(ProviderError):
    """Network error, 5xx, WebSocket drop. Transient — retry with backoff."""

    reason: Literal["rpc_unreachable"] = "rpc_unreachable"  # type: ignore[assignment]


class RateLimitError(ProviderError):
    """RPC error -32029: request rate exceeded. Bounded retry."""

    reason: Literal["rate_limited"] = "rate_limited"  # type: ignore[assignment]


class SubscriptionFailedError(ProviderError):
    """Subscription could not be established or was unexpectedly closed."""

    reason: Literal["subscription_failed"] = "subscription_failed"  # type: ignore[assignment]


class DecodeError(ProviderError):
    """SCALE decode failed for a returned value."""

    reason: Literal["decode_failed"] = "decode_failed"  # type: ignore[assignment]


class HeadUnavailableError(ChainwakeError):
    """A notified best-head hash is no longer readable.

    This is an internal scheduling signal rather than a terminal provider
    error. Head-driven runtimes skip the stale notification and await the next
    canonical head.
    """


class CUExhaustedError(ProviderError):
    """RPC error -32030: compute units exhausted. Terminal — exit budget_exhausted.

    Unlike its sibling provider errors, CU exhaustion is translated by the
    runtime into a `BudgetExhaustedPayload` with reason
    ``provider_compute_units_exhausted``; it is not emitted via
    ``ProviderErrorPayload``. No `reason` attribute is defined here because
    the parent's `ProviderErrorPayload.reason` literal does not include the
    budget-exhausted reason, and this class's `reason` is never read.
    """


class TxNotFoundInHorizonError(ProviderError):
    """`get_block_finality` search horizon exhausted without finding the tx.

    Raised when the provider has searched its entire configured search
    horizon (default 7200 blocks ≈ 24h on FAST_BLOCKS or normal block time)
    from the current chain head and did not encounter the tx hash.  The tx
    is either older than the horizon, was never included on chain, or was
    dropped from the mempool.

    Maps to `provider_error.subscription_failed` in the output payload — the
    closest existing reason category for "we tried to look up data and the
    lookup failed."  Not transient: retrying without widening the horizon
    will not produce a different result.
    """

    reason: Literal["subscription_failed"] = "subscription_failed"  # type: ignore[assignment]


class BudgetExhaustedError(ChainwakeError):
    """Watcher budget limit reached.

    `reason` maps to `BudgetExhaustedPayload.reason`:
      - "max_ru_reached": --max-ru exhausted
      - "provider_compute_units_exhausted": RPC -32030
    """

    def __init__(
        self,
        message: str,
        reason: Literal["max_ru_reached", "provider_compute_units_exhausted"],
    ) -> None:
        super().__init__(message)
        self.reason: Literal["max_ru_reached", "provider_compute_units_exhausted"] = reason


__all__ = [
    "AuthError",
    "BudgetExhaustedError",
    "CUExhaustedError",
    "ChainwakeError",
    "DecodeError",
    "HeadUnavailableError",
    "ProviderError",
    "RPCUnreachableError",
    "RateLimitError",
    "SubscriptionFailedError",
    "TxNotFoundInHorizonError",
    "UserError",
]
