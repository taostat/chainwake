"""Provider contract test pack.

Chain providers plug in by importing this module's `provider_contract_tests`
parametrised fixture and pointing it at a `ChainProvider` factory. A
`FakeProvider` runs the pack here to prove the contract tests are exercisable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Protocol, cast

import pytest

from chainwake.providers.base import (
    BlockRef,
    Cadence,
    ChainProvider,
    EpochState,
    Event,
    EventFilter,
    EventSubscriptionProvider,
    HeadSubscriptionProvider,
    ObservableValue,
    ProviderConfig,
    StorageSubscriptionProvider,
    StorageUpdate,
    TxFinalityStatus,
)

pytestmark = pytest.mark.unit


class FakeProvider:
    """In-memory ChainProvider for contract validation."""

    name: str = "fake"
    short_alias: str = "fk"

    def __init__(self) -> None:
        self._connected = False
        self._values: dict[str, ObservableValue] = {}
        self._heads: list[BlockRef] = []
        self._events: list[Event] = []
        self._storage_updates: dict[str, list[StorageUpdate]] = {}
        self._tx: dict[str, TxFinalityStatus] = {}
        self._cadence: dict[str, Cadence] = {}

    async def connect(self, config: ProviderConfig) -> None:
        if not config.rpc_url:
            raise ValueError("rpc_url required")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def read_observable(
        self,
        path: str,
        args: dict[str, object],
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        if not self._connected:
            raise RuntimeError("not connected")
        del args, at_block
        if path not in self._values:
            raise KeyError(path)
        return self._values[path]

    def epoch_netuid_for(
        self,
        observable_path: str,
        args: dict[str, object] | None = None,
    ) -> int | None:
        del args
        parts = observable_path.split(".")
        if len(parts) > 1 and parts[0] in {"subnet", "neuron"} and parts[1].isdigit():
            return int(parts[1])
        return None

    async def get_epoch_state(
        self,
        netuid: int,
        at_block: BlockRef | None = None,
    ) -> EpochState:
        del at_block
        return EpochState(
            netuid=netuid,
            block=8_119_123,
            block_hash="0xabc",
            tempo=99,
            epoch_index=42,
            last_epoch_block=8_119_100,
            next_epoch_start_block=8_119_199,
        )

    async def subscribe_heads(
        self,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[BlockRef]:
        if not self._connected:
            raise RuntimeError("not connected")
        if charge_rpc is not None:
            charge_rpc(1)
        for head in self._heads:
            yield head

    async def subscribe_events(
        self,
        event_filter: EventFilter,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[Event]:
        if not self._connected:
            raise RuntimeError("not connected")
        if charge_rpc is not None:
            charge_rpc(1)
        for event in self._events:
            if event.event_type not in event_filter.event_types:
                continue
            if not all(event.args.get(k) == v for k, v in event_filter.args_match.items()):
                continue
            yield event

    async def subscribe_storage(
        self,
        path: str,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[StorageUpdate]:
        if not self._connected:
            raise RuntimeError("not connected")
        if charge_rpc is not None:
            charge_rpc(1)
        for update in self._storage_updates.get(path, []):
            yield update

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus:
        if not self._connected:
            raise RuntimeError("not connected")
        if tx_hash not in self._tx:
            return TxFinalityStatus(tx_hash=tx_hash, level="pending")
        return self._tx[tx_hash]

    def natural_cadence_for(self, observable_path: str) -> Cadence:
        return self._cadence.get(observable_path, Cadence.PER_BLOCK)

    def seed_value(self, value: ObservableValue) -> None:
        self._values[value.path] = value
        self._cadence.setdefault(value.path, Cadence.PER_BLOCK)

    def seed_event(self, event: Event) -> None:
        self._events.append(event)

    def seed_head(self, head: BlockRef) -> None:
        self._heads.append(head)

    def seed_storage_update(self, update: StorageUpdate) -> None:
        self._storage_updates.setdefault(update.path, []).append(update)

    def seed_tx(self, status: TxFinalityStatus) -> None:
        self._tx[status.tx_hash] = status

    def seed_cadence(self, path: str, cadence: Cadence) -> None:
        self._cadence[path] = cadence


def _ts() -> datetime:
    return datetime(2026, 5, 5, 19, 42, 10, tzinfo=UTC)


def _seeded_fake() -> FakeProvider:
    provider = FakeProvider()
    provider.seed_head(BlockRef(number=8_119_124, hash="0xdef"))
    provider.seed_value(
        ObservableValue(
            path="subnet.19.pool.price",
            value=0.0432,
            block=8_119_123,
            block_hash="0xabc",
            timestamp=_ts(),
        )
    )
    provider.seed_event(
        Event(
            event_type="subnet-registered",
            raw_event="SubtensorModule.NetworkAdded",
            args={"netuid": 130, "owner_coldkey": "5Fxxx"},
            block=8_119_156,
            block_hash="0xdef",
            timestamp=_ts(),
        )
    )
    provider.seed_storage_update(
        StorageUpdate(
            path="validator.5Fxxx.commission",
            value=0.18,
            previous_value=0.15,
            block=8_119_001,
            block_hash="0xaaa",
            timestamp=_ts(),
        )
    )
    provider.seed_tx(
        TxFinalityStatus(
            tx_hash="0xabc",
            level="finalized",
            block=8_119_123,
            block_hash="0xabc",
            timestamp=_ts(),
        )
    )
    provider.seed_cadence("subnet.19.pool.price", Cadence.PER_BLOCK)
    provider.seed_cadence("validator.5Fxxx.weights", Cadence.PER_EPOCH)
    return provider


class FullProvider(
    ChainProvider,
    HeadSubscriptionProvider,
    EventSubscriptionProvider,
    StorageSubscriptionProvider,
    Protocol,
):
    """Fixture type for the contract pack's subscription-capable providers."""


ProviderFactory = Callable[[], Awaitable[FullProvider]]


@asynccontextmanager
async def _connected_provider(factory: ProviderFactory) -> AsyncIterator[FullProvider]:
    provider = await factory()
    await provider.connect(ProviderConfig(rpc_url="ws://127.0.0.1:9944"))
    try:
        yield provider
    finally:
        await provider.disconnect()


async def _make_fake() -> FullProvider:
    return cast(FullProvider, _seeded_fake())


@pytest.fixture
def fake_factory() -> ProviderFactory:
    return _make_fake


def test_runtime_checkable_protocol_recognises_impl() -> None:
    assert isinstance(FakeProvider(), ChainProvider)


def test_protocol_rejects_missing_method() -> None:
    class NotAProvider:
        name = "x"
        short_alias = "x"

    assert not isinstance(NotAProvider(), ChainProvider)


async def test_connect_requires_rpc_url() -> None:
    provider = FakeProvider()
    with pytest.raises(ValueError, match="rpc_url"):
        await provider.connect(ProviderConfig(rpc_url=""))


async def test_read_observable_returns_value(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        value = await provider.read_observable("subnet.19.pool.price", {})
        assert value.path == "subnet.19.pool.price"
        assert value.value == 0.0432
        assert value.block_hash == "0xabc"


async def test_read_observable_unknown_path_raises(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        with pytest.raises(KeyError):
            await provider.read_observable("subnet.19.nonsense", {})


async def test_read_observable_requires_connection(fake_factory: ProviderFactory) -> None:
    provider = await fake_factory()
    with pytest.raises(RuntimeError, match="not connected"):
        await provider.read_observable("subnet.19.pool.price", {})


async def test_subscribe_heads_yields_pinned_blocks(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        heads = [head async for head in provider.subscribe_heads()]
        assert heads == [BlockRef(number=8_119_124, hash="0xdef")]


async def test_subscribe_events_filters_by_type(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        events = [
            event
            async for event in provider.subscribe_events(
                EventFilter(event_types=("subnet-registered",))
            )
        ]
        assert len(events) == 1
        assert events[0].event_type == "subnet-registered"


async def test_subscribe_events_args_match(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        events = [
            event
            async for event in provider.subscribe_events(
                EventFilter(
                    event_types=("subnet-registered",),
                    args_match={"netuid": 999},
                )
            )
        ]
        assert events == []


async def test_subscribe_storage_yields_updates(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        updates = [
            update async for update in provider.subscribe_storage("validator.5Fxxx.commission")
        ]
        assert len(updates) == 1
        assert updates[0].previous_value == 0.15
        assert updates[0].value == 0.18


async def test_get_block_finality_pending_for_unknown(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        status = await provider.get_block_finality("0xunknown")
        assert status.level == "pending"
        assert status.block is None


async def test_get_block_finality_finalized(fake_factory: ProviderFactory) -> None:
    async with _connected_provider(fake_factory) as provider:
        status = await provider.get_block_finality("0xabc")
        assert status.level == "finalized"
        assert status.block == 8_119_123


def test_natural_cadence_returns_registered() -> None:
    provider = _seeded_fake()
    assert provider.natural_cadence_for("subnet.19.pool.price") == Cadence.PER_BLOCK
    assert provider.natural_cadence_for("validator.5Fxxx.weights") == Cadence.PER_EPOCH


def test_natural_cadence_default_per_block() -> None:
    provider = _seeded_fake()
    assert provider.natural_cadence_for("unmapped.observable") == Cadence.PER_BLOCK


def test_block_ref_requires_at_least_one_field() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BlockRef()
    assert BlockRef(number=1).number == 1
    assert BlockRef(hash="0xabc").hash == "0xabc"


def test_observable_value_is_frozen() -> None:
    value = ObservableValue(
        path="x",
        value=1,
        block=1,
        block_hash="0x",
        timestamp=_ts(),
    )
    with pytest.raises(FrozenInstanceError):
        ObservableValue.__setattr__(value, "path", "y")


def test_provider_config_defaults() -> None:
    config = ProviderConfig(rpc_url="ws://localhost")
    assert config.api_key is None
    assert config.timeout_seconds == 30.0
    assert config.extra == {}


# ---------------------------------------------------------------------------
# EventFilter additive predicates (amount_min, direction)
# ---------------------------------------------------------------------------


def test_event_filter_amount_min_only() -> None:
    filt = EventFilter(event_types=("transfer",), amount_min=1_000_000_000)
    assert filt.amount_min == 1_000_000_000
    assert filt.direction is None
    assert filt.direction_address is None


def test_event_filter_direction_only() -> None:
    filt = EventFilter(
        event_types=("transfer",),
        direction="in",
        direction_address="5Fxxx",
    )
    assert filt.direction == "in"
    assert filt.direction_address == "5Fxxx"
    assert filt.amount_min is None


def test_event_filter_amount_min_and_direction() -> None:
    filt = EventFilter(
        event_types=("transfer",),
        amount_min=42,
        direction="out",
        direction_address="5Bob",
    )
    assert filt.amount_min == 42
    assert filt.direction == "out"
    assert filt.direction_address == "5Bob"


def test_event_filter_direction_without_address_rejected() -> None:
    with pytest.raises(ValueError, match="direction_address"):
        EventFilter(event_types=("transfer",), direction="in")


def test_event_filter_negative_amount_min_rejected() -> None:
    with pytest.raises(ValueError, match="amount_min"):
        EventFilter(event_types=("transfer",), amount_min=-1)


def test_event_filter_amount_min_zero_allowed() -> None:
    filt = EventFilter(event_types=("transfer",), amount_min=0)
    assert filt.amount_min == 0


def test_event_filter_direction_both_requires_address() -> None:
    # `both` is a no-op direction, but the contract is uniform: if direction
    # is set at all (including `both`), an address must be present.
    with pytest.raises(ValueError, match="direction_address"):
        EventFilter(event_types=("transfer",), direction="both")
