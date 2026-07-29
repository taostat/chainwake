"""Chain-agnostic test harness Protocol.

The Bittensor harness in `local_chain.py` is the initial implementation. The
Ethereum/Anvil harness implements the same Protocol so integration tests can
target either chain through identical drivers.

Drivers map onto the six primitive shapes:

    drive_price_move           → threshold, delta
    drive_validator_silence    → liveness
    drive_subnet_registration  → event
    drive_balance_change       → state, threshold, delta
    drive_hyperparam_change    → state
    drive_tx_to_finality       → tx
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class DriverResult:
    """Result of a driver call.

    `block` is the block number at which the change landed. `extra` carries
    driver-specific context (transaction hash, new netuid, etc.) without
    forcing every harness to support every field.
    """

    block: int
    extra: dict[str, object]


@runtime_checkable
class ChainTestHarness(Protocol):
    """Protocol every test harness implements.

    Lifecycle:
        - `start()` boots the chain or attaches to an existing one.
        - `wait_until_ready()` blocks until the chain produces blocks.
        - `stop()` tears down (or detaches if `reuse=True`).
    """

    rpc_url: str

    async def start(self) -> None: ...

    async def wait_until_ready(self) -> None: ...

    async def stop(self) -> None: ...

    async def drive_price_move(self, netuid: int, pct: float) -> DriverResult: ...

    async def drive_validator_silence(self, hotkey: str, epochs: int) -> DriverResult: ...

    async def drive_subnet_registration(self) -> DriverResult: ...

    async def drive_balance_change(self, address: str, delta: int) -> DriverResult: ...

    async def drive_hyperparam_change(
        self, netuid: int, name: str, value: object
    ) -> DriverResult: ...

    async def drive_tx_to_finality(self) -> DriverResult: ...


__all__ = ["ChainTestHarness", "DriverResult"]
