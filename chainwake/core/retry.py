"""Retry policy for chainwake per spec §9.4.

Three distinct failure modes with distinct retry strategies:

1. Transient failures (ConnectionError, OSError, TimeoutError, RPCUnreachableError):
   Exponential backoff starting at 500ms, capped at 30s, retry indefinitely.
   These are excluded from the registry-estimated --max-ru observation guard.

2. Rate limiting (RateLimitError / -32029):
   Backoff 250ms x 2^attempt, capped at 60s per individual sleep, max 8
   consecutive attempts. The cap matters for per-minute rate limits
   (e.g. 10 req/min plans) where the bucket only refills after the
   60-second window resets — short backoffs just keep tripping the same
   bucket. After the eighth backed-off attempt, return a terminal signal so
   the runtime can emit rate-limit guidance instead of hot-looping.

3. Auth failure (AuthError / -32021) and CU exhaustion (CUExhaustedError / -32030):
   Terminal — raise immediately, the runtime converts to the right exit payload.

Usage:

    from chainwake.core.retry import with_transient_retry

    value = await with_transient_retry(provider.read_observable)(path, args)

`with_transient_retry` is an async-callable wrapper, not a function decorator
(tenacity handles the async retry loop internally). Call with_transient_retry(coro_fn)
to get a wrapped coroutine function; then await it like the original.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import ParamSpec, TypeVar

import tenacity

from chainwake.core.errors import ProviderError

_log = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

# Errors treated as transient (network layer — reconnect and retry)
_TRANSIENT_EXCEPTIONS = (
    ConnectionError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
)

# Maximum individual wait between transient retries (seconds)
_TRANSIENT_MAX_WAIT = 30.0

# Initial wait for transient backoff (seconds)
_TRANSIENT_INITIAL_WAIT = 0.5

# Rate-limit backoff params per spec §9.4. Doubling sequence:
# 250ms, 500ms, 1s, 2s, 4s, 8s, 16s, 32s — total ~64s across 8 attempts,
# enough to ride out a per-minute rate-limit window. Each individual sleep
# is capped at _RATE_LIMIT_MAX_WAIT_MS so the doubling doesn't shoot past
# any reasonable window duration.
_RATE_LIMIT_INITIAL_MS = 250
_RATE_LIMIT_MAX_WAIT_MS = 60_000
_RATE_LIMIT_MAX_ATTEMPTS = 8


def _is_transient(exc: BaseException) -> bool:
    """True for errors that warrant transient backoff and retry."""
    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        return True
    # ProviderError subclasses that map to rpc_unreachable are transient
    # (but NOT AuthError, CUExhaustedError, or rate-limit — handled separately)
    from chainwake.core.errors import RPCUnreachableError, SubscriptionFailedError  # noqa: PLC0415

    return isinstance(exc, (RPCUnreachableError, SubscriptionFailedError))


def _log_transient_retry(retry_state: tenacity.RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait = retry_state.next_action.sleep if retry_state.next_action else 0.0
    _log.warning(
        "Transient provider error; retrying after %.1fs (attempt %d): %s",
        wait,
        retry_state.attempt_number,
        exc,
    )


def with_transient_retry(  # noqa: UP047
    fn: Callable[_P, Coroutine[object, object, _T]],
    *,
    max_delay_seconds: float | None = None,
) -> Callable[_P, Coroutine[object, object, _T]]:
    """Wrap an async callable with transient-failure retry/backoff.

    Retries indefinitely on transient errors (ConnectionError, OSError,
    TimeoutError, RPCUnreachableError, SubscriptionFailedError). Raises
    immediately on terminal errors (AuthError, CUExhaustedError,
    BudgetExhaustedError, RateLimitError, DecodeError).

    Backoff: exponential starting at 500ms, capped at 30s.

    `max_delay_seconds`: if given, the retry loop stops after this many
    seconds total (using tenacity stop_after_delay). This prevents a
    permanently-unreachable endpoint from blocking the watcher past its
    `--max-runtime` deadline.
    """
    stop: tenacity.stop.stop_base
    if max_delay_seconds is not None:
        stop = tenacity.stop_after_delay(max_delay_seconds)
    else:
        stop = tenacity.stop_never

    @tenacity.retry(
        retry=tenacity.retry_if_exception(_is_transient),
        wait=tenacity.wait_exponential(
            multiplier=_TRANSIENT_INITIAL_WAIT,
            min=_TRANSIENT_INITIAL_WAIT,
            max=_TRANSIENT_MAX_WAIT,
        ),
        stop=stop,
        reraise=True,
        after=_log_transient_retry,
    )
    async def _wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        return await fn(*args, **kwargs)

    # Preserve the original function's identity as much as possible
    _wrapped.__name__ = getattr(fn, "__name__", "_wrapped")
    return _wrapped  # type: ignore[return-value]


class RateLimitGuard:
    """Tracks consecutive rate-limit hits and applies bounded backoff.

    Per spec §9.4: backoff 250ms x 2^attempt, max 8 consecutive attempts.
    The caller supplies its deadline-aware sleep primitive.  After exhaustion
    the guard returns ``False`` so the runtime emits a terminal rate-limit
    payload instead of hot-looping against the endpoint.
    """

    def __init__(self) -> None:
        self._consecutive = 0

    def reset(self) -> None:
        self._consecutive = 0

    async def handle(
        self,
        exc: ProviderError,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> bool:
        """Called when a rate-limit error is caught.

        Returns ``True`` after a bounded wait when the caller may retry.
        Returns ``False`` after the consecutive-attempt cap so the caller can
        stop with truthful upgrade/wait guidance rather than hammering.
        """
        self._consecutive += 1
        attempt = self._consecutive
        sleep_fn = asyncio.sleep if sleep is None else sleep
        if attempt <= _RATE_LIMIT_MAX_ATTEMPTS:
            wait_ms = min(
                _RATE_LIMIT_INITIAL_MS * (2 ** (attempt - 1)),
                _RATE_LIMIT_MAX_WAIT_MS,
            )
            wait_s = wait_ms / 1000
            _log.warning(
                "Rate limited (attempt %d/%d); backing off %.3fs: %s",
                attempt,
                _RATE_LIMIT_MAX_ATTEMPTS,
                wait_s,
                exc,
            )
            await sleep_fn(wait_s)
            return True
        _log.warning(
            "Rate limit persists after %d attempts; stopping retries: %s",
            _RATE_LIMIT_MAX_ATTEMPTS,
            exc,
        )
        return False


__all__ = [
    "RateLimitGuard",
    "with_transient_retry",
]
