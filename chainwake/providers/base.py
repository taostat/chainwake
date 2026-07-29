"""ChainProvider Protocol and supporting value types.

Every backend implements lifecycle, observable reads, and
transaction finality. Head, event, storage, and Bittensor-style epoch support
are separate runtime-checkable capability protocols so a new chain does not
need to fake concepts it does not have.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable


class Cadence(StrEnum):
    PER_BLOCK = "per_block"
    PER_EPOCH = "per_epoch"
    PER_EVENT = "per_event"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class BlockRef:
    """Reference to a specific block by number or hash."""

    number: int | None = None
    hash: str | None = None

    def __post_init__(self) -> None:
        if self.number is None and self.hash is None:
            raise ValueError("BlockRef requires at least one of number or hash")


@dataclass(frozen=True, slots=True)
class ObservableValue:
    """A single read of an observable at a specific block.

    Fields:
        path: canonical dotted observable path, e.g. `subnet.19.pool.price`.
        value: the read value. Numeric observables are floats or ints; state
            observables can be any JSON-compatible scalar / dict.
        block: block number at which the value was read.
        block_hash: hex hash of the block.
        timestamp: chain timestamp at which the block was authored.
        meta: optional structured metadata (e.g. raw substrate decode).
    """

    path: str
    value: object
    block: int
    block_hash: str
    timestamp: datetime
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EpochState:
    """Stateful epoch schedule for one subnet at a pinned block.

    Epochs are identified by the on-chain ``SubnetEpochIndex`` rather than
    projected from the block number. ``LastEpochBlock`` and the runtime API's
    next start block preserve the schedule after tempo changes and
    owner-triggered epochs.
    """

    netuid: int
    block: int
    block_hash: str
    tempo: int
    epoch_index: int
    last_epoch_block: int
    next_epoch_start_block: int | None


@dataclass(frozen=True, slots=True)
class Event:
    """A chain event emitted on a block.

    Args dict mirrors the SCALE-decoded event payload, with friendly key names
    where the registry maps them.
    """

    event_type: str
    raw_event: str
    args: dict[str, object]
    block: int
    block_hash: str
    timestamp: datetime
    extrinsic_hash: str | None = None


@dataclass(frozen=True, slots=True)
class StorageUpdate:
    """A storage-key change yielded by `subscribe_storage`."""

    path: str
    value: object
    previous_value: object | None
    block: int
    block_hash: str
    timestamp: datetime


EventDirection = Literal["in", "out", "both"]


@dataclass(frozen=True, slots=True)
class EventFilter:
    """Filter passed to `subscribe_events`.

    `event_types` are friendly registry names (e.g. `subnet-registered`,
    `transfer`). Provider implementations resolve these via the registry to
    underlying Substrate `Module.Event` strings. `args_match` is an exact-match
    AND filter applied after decoding.

    The optional `amount_min` and `direction` predicates layer on top of
    `args_match` for BalanceTransfer-shaped events:

    - `amount_min` — keep only events whose decoded `amount` (or `value`)
      arg is >= this value (rao). Must be non-negative.
    - `direction` — keep only events whose decoded `from`/`to` arg matches
      `direction_address` per the chosen direction. `direction_address`
      is required when `direction` is set; ``"both"`` is a no-op kept for
      CLI symmetry.

    New optional fields with defaults are additive: existing callers and
    provider implementations retain their behaviour.
    """

    event_types: tuple[str, ...]
    args_match: dict[str, object] = field(default_factory=dict)
    amount_min: int | None = None
    direction: EventDirection | None = None
    direction_address: str | None = None

    def __post_init__(self) -> None:
        if self.amount_min is not None and self.amount_min < 0:
            raise ValueError(f"amount_min must be non-negative, got {self.amount_min}")
        if self.direction is not None and self.direction_address is None:
            raise ValueError("direction_address is required when direction is set")


TxFinalityLevel = Literal["pending", "included", "safe", "finalized", "dropped"]
TxExecutionStatus = Literal["success", "reverted"]


@dataclass(frozen=True, slots=True)
class TxFinalityStatus:
    """Result of `get_block_finality`."""

    tx_hash: str
    level: TxFinalityLevel
    block: int | None = None
    block_hash: str | None = None
    timestamp: datetime | None = None
    confirmations: int | None = None
    execution_status: TxExecutionStatus | None = None
    gas_used: int | None = None
    effective_gas_price_wei: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Configuration for connecting to a chain provider.

    `rpc_url` is the WebSocket endpoint. `api_key` is opaque to the provider
    and forwarded as configured (header, query string, etc., per provider).
    `timeout_seconds` applies to individual RPC calls; reconnection backoff is
    handled by `chainwake.core.retry`.
    """

    rpc_url: str
    api_key: str | None = None
    timeout_seconds: float = 30.0
    extra: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class ChainProvider(Protocol):
    """Minimum protocol implemented by every chain backend.

    Implementations are expected to be async-context-manager friendly (use
    `connect` / `disconnect` directly; the runtime owns lifecycle). Methods
    raise `chainwake.core.errors.*` on failure.
    """

    name: str
    short_alias: str

    async def connect(self, config: ProviderConfig) -> None: ...

    async def disconnect(self) -> None: ...

    async def read_observable(
        self,
        path: str,
        args: dict[str, object],
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        """Read a single observable at the given block (or head)."""
        ...

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus:
        """Get finality status of a transaction."""
        ...


@runtime_checkable
class EpochProvider(Protocol):
    """Optional capability for subnet-style stateful epoch schedules."""

    def epoch_netuid_for(
        self,
        observable_path: str,
        args: dict[str, object] | None = None,
    ) -> int | None:
        """Return the subnet whose epoch drives a path and its read arguments."""
        ...

    async def get_epoch_state(
        self,
        netuid: int,
        at_block: BlockRef | None = None,
    ) -> EpochState:
        """Read one subnet's epoch state at a pinned block."""
        ...


@runtime_checkable
class HeadSubscriptionProvider(Protocol):
    """Optional capability for best-head subscriptions."""

    def subscribe_heads(
        self,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[BlockRef]:
        """Async iterator over best-chain block references.

        ``charge_rpc`` is called before each visible subscription or
        block-resolution RPC.
        """
        ...


@runtime_checkable
class EventSubscriptionProvider(Protocol):
    """Optional capability for decoded event subscriptions."""

    def subscribe_events(
        self,
        event_filter: EventFilter,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[Event]:
        """Async iterator over filtered events.

        ``charge_rpc`` is called immediately before each visible underlying RPC
        with its registry-estimated RU cost. Runtimes use it to apply the
        estimate-based observation guard inside the subscription bridge. This
        is not transport-level provider billing metering.
        """
        ...


@runtime_checkable
class StorageSubscriptionProvider(Protocol):
    """Optional capability for storage-change subscriptions."""

    def subscribe_storage(
        self,
        path: str,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[StorageUpdate]:
        """Async iterator over storage updates for a path.

        ``charge_rpc`` has the same estimate-guard semantics as
        :meth:`subscribe_events`.
        """
        ...


__all__ = [
    "BlockRef",
    "Cadence",
    "ChainProvider",
    "EpochProvider",
    "EpochState",
    "Event",
    "EventDirection",
    "EventFilter",
    "EventSubscriptionProvider",
    "HeadSubscriptionProvider",
    "ObservableValue",
    "ProviderConfig",
    "StorageSubscriptionProvider",
    "StorageUpdate",
    "TxFinalityLevel",
    "TxFinalityStatus",
]
