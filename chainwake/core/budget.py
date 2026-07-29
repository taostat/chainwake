"""Budget tracking and enforcement for chainwake watchers.

Tracks three counters across the watcher lifetime:
  - runtime_ms: elapsed milliseconds since watcher start
  - rpc_calls: successful top-level reads plus metered event-subscription RPCs
  - estimated_ru_consumed: registry-estimated observation work

Enforces two user-configurable limits:
  - max_runtime_seconds: exits with TimeoutPayload + reason "max_runtime_reached"
  - max_ru: estimate-based guard; exits with BudgetExhaustedPayload +
    reason "max_ru_reached"

In-flight async reads reserve their estimated cost so concurrently arriving
subscription work cannot preflight against the same remaining allowance. This
is not transport-level provider billing metering. Connection bootstrap,
transient retries, and RPCs hidden inside the SDK may not be represented.
"""

from __future__ import annotations

from datetime import UTC, datetime

from chainwake.core.errors import BudgetExhaustedError


class Budget:
    """Mutable counter for a single watcher run.

    Designed for single-coroutine access inside the asyncio event loop.
    Not thread-safe.
    """

    def __init__(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_ru: int | None = None,
    ) -> None:
        self._started_at: datetime = datetime.now(UTC)
        self._rpc_calls: int = 0
        self._estimated_ru_consumed: int = 0
        self._reserved_ru: int = 0
        self._max_runtime_seconds = max_runtime_seconds
        self._max_ru = max_ru

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def rpc_calls(self) -> int:
        return self._rpc_calls

    @property
    def runtime_ms(self) -> int:
        elapsed = (datetime.now(UTC) - self._started_at).total_seconds()
        return int(elapsed * 1000)

    @property
    def estimated_ru_consumed(self) -> int:
        return self._estimated_ru_consumed

    def ensure_ru_available(self, ru_cost: int = 1) -> None:
        """Fail before an observation whose registry estimate exceeds the guard."""
        if ru_cost < 1:
            raise ValueError(f"ru_cost must be >= 1, got {ru_cost}")
        projected = self._estimated_ru_consumed + self._reserved_ru + ru_cost
        if self._max_ru is not None and projected > self._max_ru:
            raise BudgetExhaustedError(
                f"--max-ru {self._max_ru} cannot fund {ru_cost} RU "
                f"after {self._estimated_ru_consumed} RU consumed "
                f"and {self._reserved_ru} RU reserved",
                "max_ru_reached",
            )

    def reserve_ru(self, ru_cost: int) -> None:
        """Atomically reserve capacity for an in-flight async read."""
        self.ensure_ru_available(ru_cost)
        self._reserved_ru += ru_cost

    def release_ru_reservation(self, ru_cost: int) -> None:
        """Release capacity after an in-flight read fails or is cancelled."""
        if ru_cost < 1 or ru_cost > self._reserved_ru:
            raise ValueError(f"cannot release {ru_cost} RU from {self._reserved_ru} RU reserved")
        self._reserved_ru -= ru_cost

    def charge_reserved_rpc_call(self, *, ru_cost: int) -> None:
        """Commit a successful in-flight read without a second preflight."""
        self.release_ru_reservation(ru_cost)
        self.charge_rpc_call(ru_cost=ru_cost)

    def charge_rpc_call(self, *, ru_cost: int = 1) -> None:
        """Record one observation and its registry-estimated chain-read cost.

        Call after a successful top-level read (not per transient retry), or
        immediately before an internally-metered event subscription RPC.
        ``ensure_ru_available`` must be called before issuing the read.

        Reaching the cap exactly is valid: the funded observation still has
        to be evaluated.  The next preflight is what reports exhaustion.
        """
        if ru_cost < 1:
            raise ValueError(f"ru_cost must be >= 1, got {ru_cost}")
        self._rpc_calls += 1
        self._estimated_ru_consumed += ru_cost

    def is_runtime_exceeded(self) -> bool:
        """True if max_runtime_seconds has elapsed."""
        if self._max_runtime_seconds is None:
            return False
        elapsed = (datetime.now(UTC) - self._started_at).total_seconds()
        return elapsed >= self._max_runtime_seconds


__all__ = ["Budget"]
