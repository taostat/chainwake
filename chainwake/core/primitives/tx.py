"""Tx primitive.

Terminal wait for a specific transaction hash to reach a requested finality
level. Fires exactly once when the tx reaches ``"included"`` or
``"finalized"`` as requested.

Stateful: once fired, subsequent evaluations return ``NoMatch`` (the runtime
exits after dispatch regardless, but defensive re-evaluation is safe).

Input type: ``ObservableValue`` where ``value`` is a ``TxFinalityStatus``
(the provider surfaces finality via ``read_observable`` on the ``tx.*``
path). ``Event`` inputs are silently ignored.

Finality ordering: ``pending`` < ``included`` < ``safe`` < ``finalized`` < ``dropped``.
Requesting ``"included"`` fires on ``"included"``, ``"safe"``, or
``"finalized"`` (stronger confidence implies inclusion). ``"dropped"`` is
terminal — the tx will never confirm; the runtime should surface an error
rather than a match. This primitive returns ``NoMatch`` for ``"dropped"``
and lets the runtime handle it via timeout or explicit error.

``Match.observed`` conforms to ``ObservedTx`` in ``output/schema.py``.
"""

from __future__ import annotations

from typing import Literal

from chainwake.core.primitives.base import Match, PrimitiveOutcome, needs_more_data, no_match
from chainwake.core.tx_hash import validate_tx_hash
from chainwake.providers.base import Event, ObservableValue, TxFinalityLevel, TxFinalityStatus

TxFinalityTarget = Literal["included", "safe", "finalized"]

_FINALITY_ORDER: dict[TxFinalityLevel, int] = {
    "pending": 0,
    "included": 1,
    "safe": 2,
    "finalized": 3,
    "dropped": -1,
}
_TARGET_RANK: dict[TxFinalityTarget, int] = {
    "included": 1,
    "safe": 2,
    "finalized": 3,
}


class TxPrimitive:
    """Wait for a transaction to reach the requested finality level.

    Args:
        tx_hash: the hex transaction hash to wait for.
        finality: ``"included"`` (in a block), ``"safe"``, or ``"finalized"``.
    """

    name: str = "tx"

    def __init__(
        self,
        *,
        tx_hash: str,
        finality: TxFinalityTarget,
        confirmations: int | None = None,
    ) -> None:
        if confirmations is not None and confirmations < 1:
            raise ValueError("confirmations must be greater than zero")
        if finality != "included" and confirmations is not None:
            raise ValueError(f"confirmations cannot be combined with {finality} finality")
        self._tx_hash = validate_tx_hash(tx_hash)
        self._finality = finality
        self._confirmations = confirmations
        self._fired = False

    def evaluate(self, observation: ObservableValue | Event) -> PrimitiveOutcome:
        """Return ``Match`` when the tx reaches the requested finality level."""
        if self._fired or not isinstance(observation, ObservableValue):
            return no_match()
        return self._check_status(observation)

    def _check_status(self, observation: ObservableValue) -> PrimitiveOutcome:  # noqa: PLR0911
        """Evaluate a ``TxFinalityStatus`` value against the target."""
        status = observation.value
        if not isinstance(status, TxFinalityStatus):
            return needs_more_data("waiting for tx finality status")

        if status.tx_hash != self._tx_hash or status.level == "dropped":
            return no_match()

        current_rank = _FINALITY_ORDER.get(status.level, 0)
        if current_rank < _TARGET_RANK[self._finality]:
            return needs_more_data(f"tx status is {status.level!r}, waiting for {self._finality!r}")

        if status.block is None or status.block_hash is None or status.timestamp is None:
            return needs_more_data("tx finality reached but block metadata not yet available")
        if self._confirmations is not None:
            if status.confirmations is None or status.confirmations < self._confirmations:
                current = status.confirmations or 0
                return needs_more_data(
                    f"tx has {current} confirmation(s), waiting for {self._confirmations}"
                )
            if (
                status.execution_status is None
                or status.gas_used is None
                or status.effective_gas_price_wei is None
            ):
                return needs_more_data("tx receipt context is not yet available")

        self._fired = True
        observed: dict[str, object] = {
            "tx_hash": status.tx_hash,
            "finality": status.level,
            "block": status.block,
            "block_hash": status.block_hash,
            "timestamp": status.timestamp.isoformat(),
        }
        if (
            status.confirmations is not None
            and status.execution_status is not None
            and status.gas_used is not None
            and status.effective_gas_price_wei is not None
        ):
            observed.update(
                confirmations=status.confirmations,
                execution_status=status.execution_status,
                gas_used=status.gas_used,
                effective_gas_price_wei=status.effective_gas_price_wei,
            )
        return Match(observed=observed)

    def reset(self) -> None:
        """Clear the fired flag."""
        self._fired = False


__all__ = ["TxFinalityTarget", "TxPrimitive"]
