"""Runtime contract for subscription-driven Ethereum transaction watches."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest

from chainwake.core.budget import Budget
from chainwake.core.primitives.tx import TxPrimitive
from chainwake.core.registry import lookup
from chainwake.core.runtime import WatcherRunner, WatcherSpec
from chainwake.output.schema import Payload, TxCondition
from chainwake.providers.base import (
    BlockRef,
    ObservableValue,
    ProviderConfig,
    TxFinalityStatus,
)

pytestmark = pytest.mark.unit

TX_HASH = f"0x{'ab' * 32}"
BLOCK_HASH = f"0x{'cd' * 32}"
TIMESTAMP = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class _RecordingAdapter:
    name = "recording"
    should_exit_after_dispatch = True

    def __init__(self) -> None:
        self.received: list[Payload] = []

    def dispatch(self, payload: Payload) -> None:
        self.received.append(payload)

    def close(self) -> None:
        return None


class _TransactionProvider:
    name = "ethereum"
    short_alias = "eth"

    def __init__(self) -> None:
        self.reads = 0
        self.subscriptions = 0

    async def connect(self, config: ProviderConfig) -> None:
        del config

    async def disconnect(self) -> None:
        return None

    async def read_observable(
        self,
        path: str,
        args: dict[str, object],
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        self.reads += 1
        included = self.reads > 1
        status = TxFinalityStatus(
            tx_hash=TX_HASH,
            level="included" if included else "pending",
            block=100 if included else None,
            block_hash=BLOCK_HASH if included else None,
            timestamp=TIMESTAMP if included else None,
            confirmations=1 if included else None,
            execution_status="success" if included else None,
            gas_used=21_000 if included else None,
            effective_gas_price_wei=1_500_000_000 if included else None,
        )
        return ObservableValue(
            path=path,
            value=status,
            block=at_block.number if at_block and at_block.number is not None else 99,
            block_hash=at_block.hash if at_block and at_block.hash is not None else BLOCK_HASH,
            timestamp=TIMESTAMP,
        )

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus:
        del tx_hash
        raise AssertionError("runtime should use read_observable")

    def subscribe_heads(
        self,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[BlockRef]:
        async def stream() -> AsyncIterator[BlockRef]:
            self.subscriptions += 1
            if charge_rpc is not None:
                charge_rpc(1)
            yield BlockRef(number=101, hash=f"0x{'ef' * 32}")

        return stream()


@pytest.mark.asyncio
async def test_tx_status_driver_reads_on_new_heads_and_emits_current_contract() -> None:
    provider = _TransactionProvider()
    adapter = _RecordingAdapter()
    spec = WatcherSpec(
        chain="eth",
        resource="tx",
        path_params={"tx_hash": TX_HASH},
        sub_resource="finality",
        primitive_name="tx",
        condition=TxCondition(finality="included", confirmations=1),
        invocation=["chainwake", "eth", "tx", TX_HASH],
        max_runtime_seconds=1,
        read_args={"finality": "included", "confirmations": 1},
    )
    runner = WatcherRunner(
        spec,
        entry=lookup("tx.{tx_hash}", chain="eth"),
        provider=provider,
        primitive=TxPrimitive(tx_hash=TX_HASH, finality="included", confirmations=1),
        adapters=[adapter],
        budget=Budget(max_runtime_seconds=1),
    )

    exit_code = await runner.run()

    assert exit_code == 0
    assert provider.reads == 2
    assert provider.subscriptions == 1
    assert len(adapter.received) == 1
    payload = adapter.received[0]
    assert payload.status == "matched"
