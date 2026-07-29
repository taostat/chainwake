"""Chain backend registration and capability boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import pytest

from chainwake.chains import (
    ChainBackend,
    ChainBackendRegistry,
    ChainRuntimeConfig,
    backend_for,
)
from chainwake.core.budget import Budget
from chainwake.core.primitives.threshold import ThresholdPrimitive
from chainwake.core.registry import RegistryEntry
from chainwake.core.runtime import WatcherRunner, WatcherSpec
from chainwake.output.schema import ThresholdCondition
from chainwake.providers.base import (
    BlockRef,
    Cadence,
    ChainProvider,
    EpochProvider,
    ObservableValue,
    ProviderConfig,
    TxFinalityStatus,
)

pytestmark = pytest.mark.unit


class _MinimalProvider:
    """A valid backend with no Bittensor-only epoch capability."""

    name = "minimal"
    short_alias = "eth"

    async def connect(self, config: ProviderConfig) -> None:
        del config

    async def disconnect(self) -> None:
        pass

    async def read_observable(
        self,
        path: str,
        args: dict[str, object],
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        del args, at_block
        return ObservableValue(
            path=path,
            value=1,
            block=1,
            block_hash="0x1",
            timestamp=datetime.now(UTC),
        )

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus:
        return TxFinalityStatus(tx_hash=tx_hash, level="pending")


class _ExitAdapter:
    name = "test"
    should_exit_after_dispatch = True

    def __init__(self) -> None:
        self.received: list[object] = []

    def dispatch(self, payload: object) -> None:
        self.received.append(payload)

    def close(self) -> None:
        pass


def test_base_provider_does_not_require_bittensor_epoch_methods() -> None:
    provider = _MinimalProvider()

    assert isinstance(provider, ChainProvider)
    assert not isinstance(provider, EpochProvider)


def test_backend_registry_selects_provider_and_runtime_by_chain() -> None:
    runtime = ChainRuntimeConfig(
        block_seconds=2.0,
        epoch_state_read_cost=0,
        event_block_read_cost=1,
        event_legacy_block_read_cost=1,
    )
    registry = ChainBackendRegistry(
        [ChainBackend(alias="eth", provider_factory=_MinimalProvider, runtime=runtime)]
    )

    assert isinstance(registry.create_provider("eth"), _MinimalProvider)
    assert registry.get("eth").runtime is runtime


def test_backend_registry_rejects_unknown_chain() -> None:
    registry = ChainBackendRegistry()

    with pytest.raises(KeyError, match="unknown chain backend"):
        registry.get("eth")


def test_builtin_bittensor_backend_owns_its_runtime_profile() -> None:
    backend = backend_for("bt")

    assert backend.alias == "bt"
    assert backend.runtime.block_seconds == 12.0
    assert backend.runtime.epoch_state_read_cost == 4


@pytest.mark.asyncio
async def test_runtime_falls_back_to_polling_without_head_capability() -> None:
    runtime = ChainRuntimeConfig(
        block_seconds=2.0,
        epoch_state_read_cost=0,
        event_block_read_cost=1,
        event_legacy_block_read_cost=1,
    )
    spec = WatcherSpec(
        chain="eth",
        resource="network",
        path_params={},
        sub_resource="height",
        primitive_name="threshold",
        condition=ThresholdCondition(operator="above", target=0.5),
        invocation=["chainwake", "eth", "network", "height"],
        max_runtime_seconds=1.0,
    )
    entry = RegistryEntry(
        chain="eth",
        path_template="network.height",
        resource="network",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=True,
        applicable_primitives=("threshold",),
        description="Ethereum height.",
    )
    adapter = _ExitAdapter()
    banner = StringIO()
    runner = WatcherRunner(
        spec,
        entry=entry,
        provider=_MinimalProvider(),
        primitive=ThresholdPrimitive(operator="above", target=0.5),
        adapters=[adapter],
        budget=Budget(max_runtime_seconds=1.0),
        banner_stream=banner,
        runtime=runtime,
    )

    assert await runner.run() == 0
    assert len(adapter.received) == 1
    assert "chainHead" not in banner.getvalue()
    assert "poll 2s" in banner.getvalue()


def test_runtime_rejects_observable_from_another_chain() -> None:
    runtime = ChainRuntimeConfig(
        block_seconds=2.0,
        epoch_state_read_cost=0,
        event_block_read_cost=1,
        event_legacy_block_read_cost=1,
    )
    spec = WatcherSpec(
        chain="eth",
        resource="network",
        path_params={},
        sub_resource="height",
        primitive_name="threshold",
        condition=ThresholdCondition(operator="above", target=0.5),
        invocation=["chainwake", "eth", "network", "height"],
        max_runtime_seconds=1.0,
    )
    entry = RegistryEntry(
        chain="bt",
        path_template="network.height",
        resource="network",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=True,
        applicable_primitives=("threshold",),
        description="Bittensor height.",
    )

    with pytest.raises(ValueError, match="does not match watcher chain"):
        WatcherRunner(
            spec,
            entry=entry,
            provider=_MinimalProvider(),
            primitive=ThresholdPrimitive(operator="above", target=0.5),
            adapters=[_ExitAdapter()],
            budget=Budget(max_runtime_seconds=1.0),
            runtime=runtime,
        )
