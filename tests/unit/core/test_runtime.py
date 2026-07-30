"""Unit tests for chainwake.core.runtime (WatcherRunner)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chainwake.chains import ChainRuntimeConfig
from chainwake.core.budget import Budget
from chainwake.core.errors import (
    AuthError,
    CUExhaustedError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
    UserError,
)
from chainwake.core.primitives.base import Match, NoMatch
from chainwake.core.primitives.delta import DeltaPrimitive
from chainwake.core.primitives.event import EventPrimitive
from chainwake.core.primitives.state import StatePrimitive
from chainwake.core.primitives.threshold import ThresholdOperator, ThresholdPrimitive
from chainwake.core.registry import ObservationDriver, lookup
from chainwake.core.retry import RateLimitGuard
from chainwake.core.runtime import (
    WatcherRunner,
    WatcherSpec,
    _estimate_ru_per_day,
    _resolve_effective_poll,
)
from chainwake.output.schema import (
    DeltaCondition,
    EventCondition,
    LivenessCondition,
    StateCondition,
    ThresholdCondition,
    Window,
    WindowUnit,
)
from chainwake.providers.base import (
    BlockRef,
    Cadence,
    EpochState,
    Event,
    EventFilter,
    ObservableValue,
    StorageUpdate,
)
from chainwake.providers.bittensor import DEFAULT_RPC_URL as BT_DEFAULT_RPC_URL
from chainwake.providers.bittensor import BittensorProvider

pytestmark = pytest.mark.unit

_BLOCK_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _make_spec(
    *,
    max_runtime_seconds: float | None = 5.0,
    poll_seconds: float | None = 0.01,
    max_ru: int | None = None,
    target: float = 1.0,
    operator: ThresholdOperator = "below",
) -> WatcherSpec:
    return WatcherSpec(
        chain="bt",
        resource="subnet",
        path_params={"netuid": "1"},
        sub_resource="pool.price",
        primitive_name="threshold",
        condition=ThresholdCondition(operator=operator, target=target),
        invocation=["chainwake", "bt", "subnet", "price", "1", "--below", str(target)],
        poll_seconds=poll_seconds,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
    )


def _with_delta_window(
    spec: WatcherSpec,
    *,
    unit: WindowUnit,
    value: str,
) -> WatcherSpec:
    """Select the condition-aware delta driver needed by scheduling tests."""
    spec.primitive_name = "delta"
    spec.condition = DeltaCondition(
        operator="move-pct",
        target=1,
        window=Window(unit=unit, value=value),
    )
    return spec


def _make_observable(value: float = 0.5) -> ObservableValue:
    return ObservableValue(
        path="subnet.1.pool.price",
        value=value,
        block=100,
        block_hash="0xabc",
        timestamp=_BLOCK_TS,
    )


def _make_observable_at_block(value: float, *, block: int) -> ObservableValue:
    return ObservableValue(
        path="subnet.1.pool.price",
        value=value,
        block=block,
        block_hash="0xabc",
        timestamp=_BLOCK_TS,
    )


def _epoch_state(
    epoch_index: int = 1,
    *,
    block: int = 100,
    last_epoch_block: int = 100,
) -> EpochState:
    return EpochState(
        netuid=1,
        block=block,
        block_hash=f"0x{block:064x}",
        tempo=99,
        epoch_index=epoch_index,
        last_epoch_block=last_epoch_block,
        next_epoch_start_block=block + 99,
    )


class _RecordingAdapter:
    """Adapter that records dispatched payloads."""

    name = "recording"
    should_exit_after_dispatch = False

    def __init__(self, *, exit_on_dispatch: bool = False) -> None:
        self.received: list[Any] = []
        self.should_exit_after_dispatch = exit_on_dispatch

    def dispatch(self, payload: Any) -> None:
        self.received.append(payload)

    def close(self) -> None:
        pass


def _make_primitive(
    *,
    match: bool = True,
    primitive_name: str = "threshold",
) -> MagicMock:
    prim = MagicMock()
    prim.name = primitive_name
    obs = _make_observable()
    if match:
        observed: dict[str, object] = {
            "path": obs.path,
            "value": float(cast(float, obs.value)),
            "block": obs.block,
            "block_hash": obs.block_hash,
            "timestamp": obs.timestamp.isoformat(),
        }
        if primitive_name == "delta":
            observed.update(previous_value=1.0, delta=-0.5, delta_pct=-50.0)
        prim.evaluate.return_value = Match(observed=observed)
    else:
        prim.evaluate.return_value = NoMatch()
    return prim


def _make_provider(
    observable: ObservableValue | None = None,
    *,
    cadence: Cadence = Cadence.PER_BLOCK,
) -> MagicMock:
    provider = MagicMock(spec=BittensorProvider)
    provider.name = "bittensor"
    provider.short_alias = "bt"
    provider.natural_cadence_for.return_value = cadence
    provider.epoch_netuid_for.return_value = 1
    provider.get_epoch_state = AsyncMock(return_value=_epoch_state())
    if observable is None:
        observable = _make_observable()
    provider.read_observable = AsyncMock(return_value=observable)
    return provider


class TestWatcherRunnerInit:
    async def test_rejects_empty_adapters(self) -> None:
        spec = _make_spec()
        entry = lookup("subnet.{netuid}.burn-rate")
        with pytest.raises(ValueError, match="at least one adapter"):
            WatcherRunner(
                spec,
                entry=entry,
                provider=_make_provider(),
                primitive=_make_primitive(),
                adapters=[],
                budget=Budget(max_runtime_seconds=5.0),
            )

    def test_registry_policy_is_the_runtime_strategy_authority(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="event",
            path_params={},
            sub_resource="transfer",
            primitive_name="event",
            condition=EventCondition(event_type="transfer"),
            invocation=["chainwake", "bt", "event", "--type", "transfer"],
        )
        runner = WatcherRunner(
            spec,
            entry=lookup("event.transfer"),
            provider=_make_provider(cadence=Cadence.PER_BLOCK),
            primitive=EventPrimitive(event_type="transfer"),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=1.0),
        )

        assert runner._observation_driver() == ObservationDriver.EVENT_STREAM


def test_runtime_timing_is_supplied_by_chain_backend() -> None:
    runtime = ChainRuntimeConfig(
        block_seconds=2.0,
        epoch_state_read_cost=0,
        event_block_read_cost=1,
        event_legacy_block_read_cost=1,
    )

    assert _resolve_effective_poll(None, runtime=runtime) == 2.0
    assert _resolve_effective_poll(None, policy_poll_seconds=60.0, runtime=runtime) == 60.0
    assert _resolve_effective_poll(5.0, policy_poll_seconds=60.0, runtime=runtime) == 5.0
    assert (
        _estimate_ru_per_day(
            Cadence.PER_EVENT,
            99.0,
            runtime=runtime,
        )
        == 43_200
    )


class TestWatcherRunnerMatch:
    async def test_exits_0_on_match_with_exit_adapter(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.burn-rate")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        assert len(adapter.received) == 1
        assert adapter.received[0].status == "matched"


class TestReadCostEnforcement:
    async def test_subnet_entity_guard_is_preflighted_before_provider_call(self) -> None:
        # Price costs Timestamp.Now + NetworksAdded + dynamic-info. A budget
        # that could fund the old two-read path must fail before any provider
        # call, not execute an unmetered entity-existence query.
        spec = _make_spec(max_ru=2, max_runtime_seconds=300.0, poll_seconds=0.0001)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        assert entry.read_cost == 3
        adapter = _RecordingAdapter()
        budget = Budget(max_runtime_seconds=300.0, max_ru=2)
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[adapter],
            budget=budget,
        )

        code = await runner.run()

        assert code == 1
        provider.read_observable.assert_not_awaited()
        assert budget.estimated_ru_consumed == 0

    async def test_exactly_funded_matching_read_is_evaluated(self) -> None:
        entry = lookup("subnet.{netuid}.pool.price")
        spec = _make_spec(
            max_ru=entry.read_cost,
            max_runtime_seconds=300.0,
            poll_seconds=0.0001,
        )
        provider = _make_provider()
        primitive = _make_primitive(match=True)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        budget = Budget(max_runtime_seconds=300.0, max_ru=entry.read_cost)
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=primitive,
            adapters=[adapter],
            budget=budget,
        )

        code = await runner.run()

        assert code == 0
        primitive.evaluate.assert_called_once()
        assert adapter.received[0].status == "matched"
        assert adapter.received[0].budget.estimated_ru_consumed == entry.read_cost


class TestEpochContextSelection:
    async def test_validator_weights_threads_explicit_netuid_into_epoch_selection(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="validator",
            path_params={"hotkey": "5Fxxx"},
            sub_resource="weights",
            primitive_name="liveness",
            condition=LivenessCondition(operator="silent-for", duration="3epochs"),
            invocation=[
                "chainwake",
                "bt",
                "validator",
                "5Fxxx",
                "weights",
                "--netuid",
                "19",
                "--silent-for",
                "3epochs",
            ],
            poll_seconds=0.0001,
            max_runtime_seconds=0.001,
            read_args={"netuid": 19, "mechid": 0},
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        runner = WatcherRunner(
            spec,
            entry=lookup("validator.{hotkey}.weights"),
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=0.001),
        )

        await runner.run()

        provider.epoch_netuid_for.assert_any_call(
            "validator.5Fxxx.weights",
            {"netuid": 19, "mechid": 0},
        )

    async def test_epoch_state_cost_is_preflighted_before_chain_reads(self) -> None:
        spec = _with_delta_window(
            _make_spec(max_ru=3, max_runtime_seconds=300.0, poll_seconds=0.0001),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        adapter = _RecordingAdapter()
        budget = Budget(max_runtime_seconds=300.0, max_ru=3)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.burn-rate"),
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[adapter],
            budget=budget,
        )

        code = await runner.run()

        assert code == 1
        provider.get_epoch_state.assert_not_awaited()
        provider.read_observable.assert_not_awaited()
        assert budget.estimated_ru_consumed == 0

    async def test_fan_out_dispatches_to_all_adapters(self) -> None:
        spec = _make_spec()
        a1 = _RecordingAdapter(exit_on_dispatch=True)
        a2 = _RecordingAdapter(exit_on_dispatch=False)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[a1, a2],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        assert len(a1.received) == 1
        assert len(a2.received) == 1


class TestStorageSubscriptionRuntime:
    @staticmethod
    def _spec(*, max_runtime_seconds: float = 1.0) -> WatcherSpec:
        return WatcherSpec(
            chain="bt",
            resource="network",
            path_params={},
            sub_resource="runtime-version",
            primitive_name="state",
            condition=StateCondition(operator="on-change"),
            invocation=[
                "chainwake",
                "bt",
                "network",
                "runtime-version",
                "--on-change",
                "--json",
            ],
            max_runtime_seconds=max_runtime_seconds,
        )

    @staticmethod
    def _version(version: int, block: int) -> ObservableValue:
        return ObservableValue(
            path="network.runtime-version",
            value={"spec_version": version, "spec_name": "subtensor"},
            block=block,
            block_hash=f"0x{block:064x}",
            timestamp=_BLOCK_TS,
        )

    def test_runtime_selects_storage_only_for_baseline_delta(self) -> None:
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        entry = lookup("subnet.{netuid}.pool.price")

        ever_spec = _make_spec()
        ever_spec.primitive_name = "delta"
        ever_spec.condition = DeltaCondition(
            operator="move-pct",
            target=1,
            window=Window(unit="ever", value="watcher-start"),
        )
        ever_runner = WatcherRunner(
            ever_spec,
            entry=entry,
            provider=provider,
            primitive=DeltaPrimitive(
                operator="move-pct",
                target=1,
                window_unit="ever",
                window_value="watcher-start",
            ),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=1.0),
        )

        epoch_spec = _make_spec()
        epoch_spec.primitive_name = "delta"
        epoch_spec.condition = DeltaCondition(
            operator="move-pct",
            target=1,
            window=Window(unit="epochs", value="1"),
        )
        epoch_runner = WatcherRunner(
            epoch_spec,
            entry=entry,
            provider=provider,
            primitive=DeltaPrimitive(
                operator="move-pct",
                target=1,
                window_unit="epochs",
                window_value="1",
            ),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=1.0),
        )

        assert ever_runner._observation_driver() == ObservationDriver.STORAGE_CHANGE
        assert epoch_runner._observation_driver() == ObservationDriver.BEST_HEAD

    async def test_parallel_storage_startup_cannot_overshoot_max_ru(self) -> None:
        baseline_started = asyncio.Event()
        finish_baseline = asyncio.Event()

        class _ChargingStream:
            closed = False

            def __init__(self, charge_rpc: Any) -> None:
                self._charge_rpc = charge_rpc

            def __aiter__(self) -> _ChargingStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                await baseline_started.wait()
                try:
                    # Two keys, subscription setup, initial notification, and
                    # its block-number lookup.
                    for _ in range(5):
                        self._charge_rpc(1)
                finally:
                    finish_baseline.set()
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        async def delayed_baseline(*_args: object) -> ObservableValue:
            baseline_started.set()
            await finish_baseline.wait()
            return _make_observable_at_block(1.0, block=100)

        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(side_effect=delayed_baseline)
        provider.subscribe_storage = MagicMock(
            side_effect=lambda _path, *, charge_rpc: _ChargingStream(charge_rpc)
        )
        adapter = _RecordingAdapter()
        spec = _make_spec(max_ru=5, max_runtime_seconds=1.0)
        budget = Budget(max_runtime_seconds=1.0, max_ru=5)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=ThresholdPrimitive(operator="below", target=0.0),
            adapters=[adapter],
            budget=budget,
        )

        code = await runner.run()

        assert code == 1
        assert adapter.received[-1].reason == "max_ru_reached"
        assert budget.estimated_ru_consumed <= 5

    async def test_storage_notification_re_reads_observable_at_notified_block(self) -> None:
        class _OneUpdateStream:
            def __init__(self) -> None:
                self.closed = False
                self.sent = False

            def __aiter__(self) -> _OneUpdateStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                if self.sent:
                    await asyncio.Event().wait()
                self.sent = True
                return StorageUpdate(
                    path="network.runtime-version",
                    # Raw subscription values are deliberately not trusted as
                    # decoded Chainwake observables.
                    value={"spec_version": 999},
                    previous_value={"spec_version": 1},
                    block=101,
                    block_hash=f"0x{101:064x}",
                    timestamp=_BLOCK_TS,
                )

            async def aclose(self) -> None:
                self.closed = True

        stream = _OneUpdateStream()
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.epoch_netuid_for.return_value = None
        provider.read_observable = AsyncMock(
            side_effect=[self._version(1, 100), self._version(2, 101)]
        )
        provider.subscribe_storage = MagicMock(return_value=stream)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            self._spec(),
            entry=lookup("network.runtime-version"),
            provider=provider,
            primitive=StatePrimitive(operator="on-change"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=1.0),
        )

        code = await runner.run()

        assert code == 0
        assert stream.closed is True
        assert provider.read_observable.await_args_list[0].args == (
            "network.runtime-version",
            {},
        )
        assert provider.read_observable.await_args_list[1].args == (
            "network.runtime-version",
            {},
            BlockRef(number=101, hash=f"0x{101:064x}"),
        )
        assert adapter.received[-1].observed.value == {
            "spec_version": 2,
            "spec_name": "subtensor",
        }

    async def test_unsupported_storage_uses_registry_fallback_driver(self) -> None:
        class _UnsupportedStream:
            closed = False

            def __aiter__(self) -> _UnsupportedStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                raise NotImplementedError("storage subscription unavailable")

            async def aclose(self) -> None:
                self.closed = True

        stream = _UnsupportedStream()
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.epoch_netuid_for.return_value = None
        provider.read_observable = AsyncMock(
            side_effect=[self._version(1, 100), self._version(2, 101)]
        )
        provider.subscribe_storage = MagicMock(return_value=stream)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            self._spec(),
            entry=lookup("network.runtime-version"),
            provider=provider,
            primitive=StatePrimitive(operator="on-change"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=1.0),
        )

        with (
            patch.object(
                runner,
                "_head_subscription_loop",
                new_callable=AsyncMock,
                return_value=0,
            ) as head_loop,
            patch.object(
                runner,
                "_poll_loop",
                new_callable=AsyncMock,
                return_value=0,
            ) as poll_loop,
        ):
            code = await runner.run()

        assert code == 0
        assert stream.closed is True
        provider.subscribe_storage.assert_called_once()
        provider.read_observable.assert_awaited_once()
        head_loop.assert_awaited_once()
        poll_loop.assert_not_awaited()

    async def test_silent_storage_subscription_honours_runtime_limit(self) -> None:
        class _SilentStream:
            closed = False

            def __aiter__(self) -> _SilentStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        stream = _SilentStream()
        provider = _make_provider(
            observable=self._version(1, 100),
            cadence=Cadence.PER_BLOCK,
        )
        provider.epoch_netuid_for.return_value = None
        provider.subscribe_storage = MagicMock(return_value=stream)
        adapter = _RecordingAdapter()
        runner = WatcherRunner(
            self._spec(max_runtime_seconds=0.02),
            entry=lookup("network.runtime-version"),
            provider=provider,
            primitive=StatePrimitive(operator="on-change"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=0.02),
        )

        code = await asyncio.wait_for(runner.run(), timeout=0.2)

        assert code == 1
        assert stream.closed is True
        assert adapter.received[-1].status == "timeout"
        provider.read_observable.assert_awaited_once()

    async def test_baseline_timeout_consumes_parallel_subscription_failure(self) -> None:
        class _FailedSetupStream:
            closed = False

            def __aiter__(self) -> _FailedSetupStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                raise RateLimitError("subscription setup was rate-limited")

            async def aclose(self) -> None:
                self.closed = True

        async def never_returns(*_args: object) -> ObservableValue:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        stream = _FailedSetupStream()
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.epoch_netuid_for.return_value = None
        provider.read_observable = AsyncMock(side_effect=never_returns)
        provider.subscribe_storage = MagicMock(return_value=stream)
        runner = WatcherRunner(
            self._spec(max_runtime_seconds=0.02),
            entry=lookup("network.runtime-version"),
            provider=provider,
            primitive=StatePrimitive(operator="on-change"),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=0.02),
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            code = await asyncio.wait_for(runner.run(), timeout=0.2)
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert code == 1
        assert stream.closed is True
        assert not [
            context
            for context in unhandled
            if context.get("message") == "Task exception was never retrieved"
        ]

    async def test_transient_storage_failure_reconnects_without_losing_baseline(self) -> None:
        class _FailedStream:
            closed = False

            def __aiter__(self) -> _FailedStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                raise SubscriptionFailedError("websocket closed")

            async def aclose(self) -> None:
                self.closed = True

        class _RecoveredStream:
            closed = False

            def __aiter__(self) -> _RecoveredStream:
                return self

            async def __anext__(self) -> StorageUpdate:
                return StorageUpdate(
                    path="network.runtime-version",
                    value={"spec_version": 2},
                    previous_value={"spec_version": 1},
                    block=101,
                    block_hash=f"0x{101:064x}",
                    timestamp=_BLOCK_TS,
                )

            async def aclose(self) -> None:
                self.closed = True

        failed = _FailedStream()
        recovered = _RecoveredStream()
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.epoch_netuid_for.return_value = None
        provider.read_observable = AsyncMock(
            side_effect=[self._version(1, 100), self._version(2, 101)]
        )
        provider.subscribe_storage = MagicMock(side_effect=[failed, recovered])
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            self._spec(),
            entry=lookup("network.runtime-version"),
            provider=provider,
            primitive=StatePrimitive(operator="on-change"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=1.0),
        )

        with patch.object(runner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 0
        assert provider.subscribe_storage.call_count == 2
        assert provider.read_observable.await_count == 2
        assert failed.closed is True
        assert recovered.closed is True

    def test_rolling_delta_stays_polled_for_subscribable_key(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="account",
            path_params={"coldkey": "5Fxxx"},
            sub_resource="balance",
            primitive_name="delta",
            condition=DeltaCondition(
                operator="move-pct",
                target=1.0,
                window=Window(unit="time", value="1h"),
            ),
            invocation=[
                "chainwake",
                "bt",
                "account",
                "5Fxxx",
                "balance",
                "--move-pct",
                "1",
                "--window-time",
                "1h",
            ],
        )
        runner = WatcherRunner(
            spec,
            entry=lookup("account.{coldkey}.balance"),
            provider=_make_provider(cadence=Cadence.PER_BLOCK),
            primitive=DeltaPrimitive(
                operator="move-pct",
                target=1.0,
                window_unit="time",
                window_value="1h",
            ),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=1.0),
        )

        assert runner._prefers_storage_subscription(Cadence.PER_BLOCK) is False


class TestHeadSubscriptionRuntime:
    async def test_silent_head_subscription_honours_runtime_limit(self) -> None:
        class _SilentHeadStream:
            closed = False

            def __aiter__(self) -> _SilentHeadStream:
                return self

            async def __anext__(self) -> BlockRef:
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        baseline = ObservableValue(
            path="subnet.1.pool.price",
            value=0.5,
            block=100,
            block_hash=f"0x{100:064x}",
            timestamp=_BLOCK_TS,
        )
        stream = _SilentHeadStream()
        provider = _make_provider(baseline, cadence=Cadence.PER_BLOCK)
        provider.subscribe_heads = MagicMock(return_value=stream)
        adapter = _RecordingAdapter()
        runner = WatcherRunner(
            _make_spec(poll_seconds=None, max_runtime_seconds=0.02, target=0.4),
            entry=lookup("subnet.{netuid}.pool.moving-price"),
            provider=provider,
            primitive=ThresholdPrimitive(operator="below", target=0.4),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=0.02),
        )

        code = await asyncio.wait_for(runner.run(), timeout=0.2)

        assert code == 1
        assert stream.closed is True
        assert adapter.received[-1].status == "timeout"
        provider.read_observable.assert_awaited_once()

    async def test_new_head_drives_pinned_per_block_read(self) -> None:
        class _OneHeadStream:
            closed = False
            sent = False

            def __aiter__(self) -> _OneHeadStream:
                return self

            async def __anext__(self) -> BlockRef:
                if self.sent:
                    await asyncio.Event().wait()
                self.sent = True
                return BlockRef(number=101, hash=f"0x{101:064x}")

            async def aclose(self) -> None:
                self.closed = True

        baseline = ObservableValue(
            path="subnet.1.pool.price",
            value=0.5,
            block=100,
            block_hash=f"0x{100:064x}",
            timestamp=_BLOCK_TS,
        )
        changed = ObservableValue(
            path="subnet.1.pool.price",
            value=0.3,
            block=101,
            block_hash=f"0x{101:064x}",
            timestamp=_BLOCK_TS,
        )
        stream = _OneHeadStream()
        provider = _make_provider(baseline, cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(side_effect=[baseline, changed])
        provider.subscribe_heads = MagicMock(return_value=stream)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            _make_spec(poll_seconds=None, target=0.4),
            entry=lookup("subnet.{netuid}.pool.moving-price"),
            provider=provider,
            primitive=ThresholdPrimitive(operator="below", target=0.4),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=1.0),
        )

        code = await runner.run()

        assert code == 0
        assert stream.closed is True
        provider.subscribe_heads.assert_called_once()
        assert provider.read_observable.await_args_list[1].args == (
            "subnet.1.pool.moving-price",
            {},
            BlockRef(number=101, hash=f"0x{101:064x}"),
        )
        assert adapter.received[-1].observed.value == 0.3

    async def test_unsupported_head_subscription_falls_back_to_polling(self) -> None:
        class _UnsupportedHeadStream:
            closed = False

            def __aiter__(self) -> _UnsupportedHeadStream:
                return self

            async def __anext__(self) -> BlockRef:
                raise NotImplementedError("new-head subscription unavailable")

            async def aclose(self) -> None:
                self.closed = True

        stream = _UnsupportedHeadStream()
        baseline = ObservableValue(
            path="subnet.1.pool.price",
            value=0.5,
            block=100,
            block_hash=f"0x{100:064x}",
            timestamp=_BLOCK_TS,
        )
        changed = ObservableValue(
            path="subnet.1.pool.price",
            value=0.3,
            block=101,
            block_hash=f"0x{101:064x}",
            timestamp=_BLOCK_TS,
        )
        provider = _make_provider(baseline, cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(side_effect=[baseline, changed])
        provider.subscribe_heads = MagicMock(return_value=stream)
        runner = WatcherRunner(
            _make_spec(poll_seconds=None, target=0.4),
            entry=lookup("subnet.{netuid}.pool.moving-price"),
            provider=provider,
            primitive=ThresholdPrimitive(operator="below", target=0.4),
            adapters=[_RecordingAdapter(exit_on_dispatch=True)],
            budget=Budget(max_runtime_seconds=1.0),
        )

        code = await runner.run()

        assert code == 0
        assert stream.closed is True
        assert provider.read_observable.await_count == 2

    async def test_transient_head_failure_reconnects(self) -> None:
        class _FailedHeadStream:
            closed = False

            def __aiter__(self) -> _FailedHeadStream:
                return self

            async def __anext__(self) -> BlockRef:
                raise SubscriptionFailedError("websocket closed")

            async def aclose(self) -> None:
                self.closed = True

        class _RecoveredHeadStream:
            closed = False

            def __aiter__(self) -> _RecoveredHeadStream:
                return self

            async def __anext__(self) -> BlockRef:
                return BlockRef(number=101, hash=f"0x{101:064x}")

            async def aclose(self) -> None:
                self.closed = True

        baseline = ObservableValue(
            path="subnet.1.pool.price",
            value=0.5,
            block=100,
            block_hash=f"0x{100:064x}",
            timestamp=_BLOCK_TS,
        )
        changed = ObservableValue(
            path="subnet.1.pool.price",
            value=0.3,
            block=101,
            block_hash=f"0x{101:064x}",
            timestamp=_BLOCK_TS,
        )
        failed = _FailedHeadStream()
        recovered = _RecoveredHeadStream()
        provider = _make_provider(baseline, cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(side_effect=[baseline, changed])
        provider.subscribe_heads = MagicMock(side_effect=[failed, recovered])
        runner = WatcherRunner(
            _make_spec(poll_seconds=None, target=0.4),
            entry=lookup("subnet.{netuid}.pool.moving-price"),
            provider=provider,
            primitive=ThresholdPrimitive(operator="below", target=0.4),
            adapters=[_RecordingAdapter(exit_on_dispatch=True)],
            budget=Budget(max_runtime_seconds=1.0),
        )

        with patch.object(runner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 0
        assert provider.subscribe_heads.call_count == 2
        assert failed.closed is True
        assert recovered.closed is True

    def test_explicit_poll_interval_opts_out_of_head_subscription(self) -> None:
        runner = WatcherRunner(
            _make_spec(poll_seconds=60.0),
            entry=lookup("subnet.{netuid}.pool.moving-price"),
            provider=_make_provider(cadence=Cadence.PER_BLOCK),
            primitive=ThresholdPrimitive(operator="below", target=0.4),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=1.0),
        )

        assert runner._prefers_head_subscription(Cadence.PER_BLOCK) is False


class TestEventSubscriptionRuntime:
    async def test_internal_event_rpcs_cannot_exceed_hard_ru_cap(self) -> None:
        class _NeverStartedStream:
            def __aiter__(self) -> _NeverStartedStream:
                return self

            async def __anext__(self) -> Any:
                raise AssertionError("stream must not start after budget exhaustion")

        provider = _make_provider(cadence=Cadence.PER_EVENT)

        def subscribe_events(
            _event_filter: EventFilter,
            *,
            charge_rpc: Any,
        ) -> _NeverStartedStream:
            # Subscription setup + two block RPCs fit. The next per-block RPC
            # must fail before it executes rather than overshooting max_ru.
            charge_rpc(1)
            charge_rpc(1)
            charge_rpc(1)
            charge_rpc(1)
            return _NeverStartedStream()

        provider.subscribe_events.side_effect = subscribe_events
        spec = WatcherSpec(
            chain="bt",
            resource="event",
            path_params={},
            sub_resource="transfer",
            primitive_name="event",
            condition=EventCondition(event_type="transfer"),
            invocation=["chainwake", "bt", "event", "--type", "transfer"],
            max_runtime_seconds=1.0,
            max_ru=3,
            event_filter=EventFilter(event_types=("transfer",)),
        )
        adapter = _RecordingAdapter()
        budget = Budget(max_runtime_seconds=1.0, max_ru=3)
        runner = WatcherRunner(
            spec,
            entry=lookup("event.transfer"),
            provider=provider,
            primitive=EventPrimitive(event_type="transfer"),
            adapters=[adapter],
            budget=budget,
        )

        code = await runner.run()

        assert code == 1
        assert adapter.received[-1].status == "budget_exhausted"
        assert adapter.received[-1].reason == "max_ru_reached"
        assert budget.estimated_ru_consumed == 3

    async def test_silent_stream_honours_max_runtime_and_closes(self) -> None:
        import asyncio  # noqa: PLC0415

        class _SilentStream:
            def __init__(self) -> None:
                self.closed = False

            def __aiter__(self) -> _SilentStream:
                return self

            async def __anext__(self) -> Any:
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        stream = _SilentStream()
        provider = _make_provider(cadence=Cadence.PER_EVENT)
        provider.subscribe_events.return_value = stream
        spec = WatcherSpec(
            chain="bt",
            resource="event",
            path_params={},
            sub_resource="transfer",
            primitive_name="event",
            condition=EventCondition(event_type="transfer"),
            invocation=["chainwake", "bt", "event", "--type", "transfer"],
            max_runtime_seconds=0.02,
            event_filter=EventFilter(event_types=("transfer",)),
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            spec,
            entry=lookup("event.transfer"),
            provider=provider,
            primitive=EventPrimitive(event_type="transfer"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=0.02),
        )

        code = await asyncio.wait_for(runner.run(), timeout=0.2)

        assert code == 1
        assert adapter.received[-1].status == "timeout"
        assert stream.closed is True
        provider.read_observable.assert_not_awaited()

    async def test_reconnects_after_transient_subscription_failure(self) -> None:
        class _FailingStream:
            closed = False

            def __aiter__(self) -> _FailingStream:
                return self

            async def __anext__(self) -> Any:
                raise SubscriptionFailedError("websocket closed")

            async def aclose(self) -> None:
                self.closed = True

        class _EventStream:
            closed = False

            def __aiter__(self) -> _EventStream:
                return self

            async def __anext__(self) -> Any:
                return Event(
                    event_type="transfer",
                    raw_event="Balances.Transfer",
                    args={"from": "5Alice", "to": "5Bob", "amount": 1},
                    block=100,
                    block_hash="0xabc",
                    timestamp=_BLOCK_TS,
                )

            async def aclose(self) -> None:
                self.closed = True

        failed = _FailingStream()
        recovered = _EventStream()
        provider = _make_provider(cadence=Cadence.PER_EVENT)
        provider.subscribe_events.side_effect = [failed, recovered]
        spec = WatcherSpec(
            chain="bt",
            resource="event",
            path_params={},
            sub_resource="transfer",
            primitive_name="event",
            condition=EventCondition(event_type="transfer"),
            invocation=["chainwake", "bt", "event", "--type", "transfer"],
            max_runtime_seconds=1.0,
            event_filter=EventFilter(event_types=("transfer",)),
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            spec,
            entry=lookup("event.transfer"),
            provider=provider,
            primitive=EventPrimitive(event_type="transfer"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=1.0),
        )

        with patch.object(runner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 0
        assert provider.subscribe_events.call_count == 2
        assert failed.closed is True
        assert recovered.closed is True
        provider.read_observable.assert_not_awaited()

    async def test_persistent_subscription_rate_limit_stops_retrying(self) -> None:
        provider = _make_provider(cadence=Cadence.PER_EVENT)
        provider.subscribe_events.side_effect = RateLimitError("free-tier limit")
        spec = WatcherSpec(
            chain="bt",
            resource="event",
            path_params={},
            sub_resource="transfer",
            primitive_name="event",
            condition=EventCondition(event_type="transfer"),
            invocation=["chainwake", "bt", "event", "--type", "transfer"],
            max_runtime_seconds=300.0,
            event_filter=EventFilter(event_types=("transfer",)),
        )
        adapter = _RecordingAdapter()
        runner = WatcherRunner(
            spec,
            entry=lookup("event.transfer"),
            provider=provider,
            primitive=EventPrimitive(event_type="transfer"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
        )

        with patch.object(RateLimitGuard, "handle", new_callable=AsyncMock) as handle:
            handle.return_value = False
            code = await asyncio.wait_for(runner.run(), timeout=0.1)

        assert code == 3
        assert adapter.received[-1].status == "provider_error"
        assert adapter.received[-1].reason == "rate_limited"
        assert provider.subscribe_events.call_count == 1

    async def test_shutdown_emits_truthful_stopped_payload(self) -> None:
        import asyncio  # noqa: PLC0415

        class _SilentStream:
            closed = False

            def __aiter__(self) -> _SilentStream:
                return self

            async def __anext__(self) -> Any:
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        stream = _SilentStream()
        provider = _make_provider(cadence=Cadence.PER_EVENT)
        provider.subscribe_events.return_value = stream
        spec = WatcherSpec(
            chain="bt",
            resource="event",
            path_params={},
            sub_resource="transfer",
            primitive_name="event",
            condition=EventCondition(event_type="transfer"),
            invocation=["chainwake", "bt", "event", "--type", "transfer"],
            max_runtime_seconds=None,
            event_filter=EventFilter(event_types=("transfer",)),
        )
        adapter = _RecordingAdapter()
        runner = WatcherRunner(
            spec,
            entry=lookup("event.transfer"),
            provider=provider,
            primitive=EventPrimitive(event_type="transfer"),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=None),
        )

        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0)
        runner._shutdown_event.set()
        code = await asyncio.wait_for(task, timeout=0.2)

        assert code == 1
        assert adapter.received[-1].status == "stopped"
        assert adapter.received[-1].reason == "shutdown_requested"
        assert stream.closed is True


class TestRuntimeBoundedSleep:
    async def test_hanging_provider_call_is_cancelled_at_watcher_deadline(self) -> None:
        async def hang_forever(*_args: object, **_kwargs: object) -> ObservableValue:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        spec = _make_spec(max_runtime_seconds=0.02, poll_seconds=0.0001)
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=hang_forever)
        adapter = _RecordingAdapter()
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.burn-rate"),
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=0.02),
        )

        code = await asyncio.wait_for(runner.run(), timeout=0.2)

        assert code == 1
        assert adapter.received[-1].status == "timeout"
        assert adapter.received[-1].reason == "max_runtime_reached"
        assert provider.read_observable.await_count == 1

    async def test_poll_sleep_never_exceeds_remaining_runtime(self) -> None:
        import asyncio  # noqa: PLC0415

        spec = _make_spec(max_runtime_seconds=0.02, poll_seconds=12.0)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=_make_provider(),
            primitive=_make_primitive(match=False),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=0.02),
        )

        await asyncio.wait_for(runner._interruptible_sleep(12.0), timeout=0.2)

        assert runner._budget.is_runtime_exceeded()

    async def test_keeps_alive_when_no_exit_adapter(self) -> None:
        """Watcher continues polling when no adapter wants to exit."""
        spec = _make_spec(max_runtime_seconds=None, poll_seconds=0.0001)
        adapter = _RecordingAdapter(exit_on_dispatch=False)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        budget = Budget(max_runtime_seconds=0.001)  # expires immediately
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=budget,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        # At least one match dispatched; watcher exits on timeout (match_count > 0 → 0)
        assert code in (0, 1)


class TestWatcherRunnerTimeout:
    async def test_returns_1_on_timeout_with_no_match(self) -> None:
        spec = _make_spec(max_runtime_seconds=0.001, poll_seconds=0.0001)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        budget = Budget(max_runtime_seconds=0.001)

        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[adapter],
            budget=budget,
        )
        # Force is_runtime_exceeded to return True on first check
        budget._max_runtime_seconds = 0.0  # type: ignore[attr-defined]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 1
        assert adapter.received[0].status == "timeout"
        assert adapter.received[0].reason == "max_runtime_reached"


class TestPortableSignalHandling:
    async def test_unsupported_event_loop_signal_handlers_do_not_block_watcher(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = _make_spec(max_runtime_seconds=0.001)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=_make_provider(),
            primitive=_make_primitive(match=False),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=0.001),
        )
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(NotImplementedError),
        )

        code = await runner.run()

        assert code == 1

    async def test_task_cancellation_emits_stopped_when_signals_are_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def hang_forever(*_args: object, **_kwargs: object) -> ObservableValue:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        spec = _make_spec(max_runtime_seconds=None)
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=hang_forever)
        adapter = _RecordingAdapter()
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=None),
        )
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(NotImplementedError),
        )

        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0)
        task.cancel()
        code = await asyncio.wait_for(task, timeout=0.2)

        assert code == 1
        assert adapter.received[-1].status == "stopped"
        assert adapter.received[-1].reason == "shutdown_requested"


class TestWatcherRunnerErrors:
    async def test_auth_error_returns_3_with_provider_error(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=AuthError("bad key"))
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 3
        assert adapter.received[0].status == "provider_error"
        assert adapter.received[0].reason == "auth_failed"

    async def test_cu_exhausted_returns_1_budget_exhausted(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=CUExhaustedError("out of CUs"))
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 1
        assert adapter.received[0].status == "budget_exhausted"
        assert adapter.received[0].reason == "provider_compute_units_exhausted"

    async def test_max_ru_exhausted_returns_1_budget_exhausted(self) -> None:
        spec = _make_spec(max_ru=1)
        adapter = _RecordingAdapter()
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0, max_ru=1),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 1
        assert adapter.received[0].status == "budget_exhausted"
        assert adapter.received[0].reason == "max_ru_reached"

    async def test_rpc_unreachable_retries_then_succeeds(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        provider.read_observable = AsyncMock(
            side_effect=[
                RPCUnreachableError("ws down"),
                RPCUnreachableError("ws down"),
                _make_observable(0.5),
            ]
        )
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        assert provider.read_observable.await_count == 3

    async def test_rate_limit_applies_backoff_then_continues(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        provider.read_observable = AsyncMock(
            side_effect=[
                RateLimitError("rate limited"),
                _make_observable(0.5),
            ]
        )
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0

    async def test_rate_limit_before_first_success_does_not_corrupt_ever_baseline(self) -> None:
        spec = _make_spec(poll_seconds=0.0001)
        spec.primitive_name = "delta"
        spec.condition = DeltaCondition(
            operator="move-pct",
            target=1.0,
            window=Window(unit="ever", value="watcher-start"),
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        provider.read_observable = AsyncMock(
            side_effect=[
                RateLimitError("rate limited"),
                _make_observable(100.0),
                _make_observable(99.0),
            ]
        )

        async def one_storage_change() -> Any:
            yield StorageUpdate(
                path="subnet.1.pool.price",
                value=99.0,
                previous_value=100.0,
                block=101,
                block_hash=f"0x{101:064x}",
                timestamp=_BLOCK_TS,
            )
            await asyncio.Event().wait()

        provider.subscribe_storage = MagicMock(return_value=one_storage_change())
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=DeltaPrimitive(
                operator="move-pct",
                target=1.0,
                window_unit="ever",
                window_value="watcher-start",
            ),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )

        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 0
        assert provider.read_observable.await_count == 3
        assert adapter.received[0].observed.previous_value == 100.0
        assert adapter.received[0].observed.value == 99.0

    async def test_rate_limit_backoff_cannot_overrun_runtime_deadline(self) -> None:
        spec = _make_spec(max_runtime_seconds=0.02, poll_seconds=0.0001)
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=RateLimitError("free-tier limit"))
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=0.02),
        )

        code = await asyncio.wait_for(runner.run(), timeout=0.1)

        assert code == 1
        assert adapter.received[0].status == "timeout"
        assert provider.read_observable.await_count == 1

    async def test_persistent_rate_limit_on_default_endpoint_suggests_signup(self) -> None:
        spec = _make_spec(max_runtime_seconds=300.0, poll_seconds=0.0001)
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=RateLimitError("free-tier limit"))
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
            rpc_url=BT_DEFAULT_RPC_URL,
        )

        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 3
        assert adapter.received[0].status == "provider_error"
        assert adapter.received[0].reason == "rate_limited"
        assert provider.read_observable.await_count == 9
        assert "sign up" in adapter.received[0].message
        assert "blockmachine.io" in adapter.received[0].message
        assert "--api-key" in adapter.received[0].message

    async def test_persistent_rate_limit_with_api_key_suggests_upgrade(self) -> None:
        spec = _make_spec(max_runtime_seconds=300.0, poll_seconds=0.0001)
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=RateLimitError("free-tier limit"))
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
            rpc_url=BT_DEFAULT_RPC_URL,
            api_key="already-have-one",
        )

        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 3
        assert "upgrade" in adapter.received[0].message.lower()
        assert "sign up" not in adapter.received[0].message
        assert "blockmachine.io" in adapter.received[0].message

    async def test_persistent_rate_limit_on_custom_endpoint_has_no_blockmachine_hint(
        self,
    ) -> None:
        spec = _make_spec(max_runtime_seconds=300.0, poll_seconds=0.0001)
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=RateLimitError("free-tier limit"))
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
            rpc_url="wss://my-own-node.example",
        )

        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()

        assert code == 3
        assert adapter.received[0].message == "free-tier limit"
        assert "blockmachine" not in adapter.received[0].message.lower()

    async def test_unexpected_exception_returns_4_internal_error(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter()
        provider = _make_provider()
        provider.read_observable = AsyncMock(side_effect=ValueError("unexpected"))
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 4
        assert adapter.received[0].status == "internal_error"

    async def test_failed_adapter_dispatch_does_not_abort_other_adapters(self) -> None:
        spec = _make_spec()
        good = _RecordingAdapter(exit_on_dispatch=True)
        bad = MagicMock()
        bad.name = "bad"
        bad.should_exit_after_dispatch = False
        bad.dispatch.side_effect = RuntimeError("dispatch failed")
        bad.close.return_value = None
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[bad, good],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        assert len(good.received) == 1


class TestWatcherSpecPathParams:
    """Multi-param path rendering — neuron = netuid + hotkey."""

    def test_two_param_spec_renders_path(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="neuron",
            path_params={"netuid": "19", "hotkey": "5Fxxx"},
            sub_resource="incentive",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="above", target=0.5),
            invocation=["chainwake", "bt", "neuron", "incentive", "19", "5Fxxx"],
        )
        entry = lookup("neuron.{netuid}.{hotkey}.incentive")
        assert entry.render_path(spec.path_params) == "neuron.19.5Fxxx.incentive"

    def test_two_param_spec_resource_id_is_dot_joined(self) -> None:
        from chainwake.core.runtime import build_watcher  # noqa: PLC0415

        spec = WatcherSpec(
            chain="bt",
            resource="neuron",
            path_params={"netuid": "19", "hotkey": "5Fxxx"},
            sub_resource="incentive",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="above", target=0.5),
            invocation=[],
        )
        watcher = build_watcher(spec)
        assert watcher.resource_id == "19.5Fxxx"
        assert watcher.resource == "neuron"

    def test_no_params_spec_resource_id_is_none(self) -> None:
        from chainwake.core.runtime import build_watcher  # noqa: PLC0415

        spec = WatcherSpec(
            chain="bt",
            resource="network",
            path_params={},
            sub_resource="subnet-registration-cost",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="above", target=0.0),
            invocation=[],
        )
        watcher = build_watcher(spec)
        assert watcher.resource_id is None

    def test_name_threads_through_to_payload(self) -> None:
        from chainwake.core.runtime import build_watcher  # noqa: PLC0415

        spec = WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "19"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="below", target=0.5),
            invocation=[],
            name="my-watcher",
        )
        watcher = build_watcher(spec)
        assert watcher.name == "my-watcher"

    def test_default_name_is_none(self) -> None:
        from chainwake.core.runtime import build_watcher  # noqa: PLC0415

        spec = _make_spec()
        watcher = build_watcher(spec)
        assert watcher.name is None

    async def test_match_payload_contains_name(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="below", target=1.0),
            invocation=[],
            name="my-watcher",
            poll_seconds=0.0001,
            max_runtime_seconds=5.0,
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        assert adapter.received[0].watcher.name == "my-watcher"

    async def test_runner_renders_multi_param_path_to_provider(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="neuron",
            path_params={"netuid": "19", "hotkey": "5Fxxx"},
            sub_resource="incentive",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="above", target=0.0),
            invocation=[],
            poll_seconds=0.0001,
            max_runtime_seconds=5.0,
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        entry = lookup("neuron.{netuid}.{hotkey}.incentive")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        # Provider received the rendered (substituted) path, not the template.
        provider.read_observable.assert_awaited()
        call_path = provider.read_observable.await_args.args[0]
        assert call_path == "neuron.19.5Fxxx.incentive"


class TestWatcherSpecReadArgs:
    """Computed observables (e.g. depth-for-trade) thread args via WatcherSpec."""

    def test_default_read_args_is_empty_dict(self) -> None:
        spec = _make_spec()
        assert spec.read_args == {}

    async def test_runner_forwards_read_args_to_provider(self) -> None:
        spec = WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "19"},
            sub_resource="pool.depth-for-trade",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="above", target=0.0),
            invocation=[],
            poll_seconds=0.0001,
            max_runtime_seconds=5.0,
            read_args={"size": 100.0, "max_bps": 50.0},
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.depth-for-trade")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        provider.read_observable.assert_awaited()
        call_args_dict = provider.read_observable.await_args.args[1]
        assert call_args_dict == {"size": 100.0, "max_bps": 50.0}

    async def test_runner_forwards_empty_read_args_when_unset(self) -> None:
        spec = _make_spec()
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await runner.run()
        provider.read_observable.assert_awaited()
        call_args_dict = provider.read_observable.await_args.args[1]
        assert call_args_dict == {}


class TestWatcherRunnerAsyncDispatch:
    """Adapter dispatch must not block the watcher's event loop.

    AppriseAdapter calls apprise's blocking sync notify() inside dispatch().
    The runtime offloads every dispatch onto ``asyncio.to_thread`` so other
    coroutines (signal handlers, timers, the next tick) can interleave while
    a slow notification is in flight.
    """

    async def test_slow_blocking_dispatch_does_not_block_concurrent_task(self) -> None:
        """A blocking sync dispatch yields the loop to a sentinel coroutine.

        If ``_dispatch`` ran the adapter on the event-loop thread, the
        sentinel could not observe the dispatch in flight — its run would
        be serialised after the blocking sleep returned. With
        ``asyncio.to_thread`` the sentinel fires while the dispatch is
        still sleeping in a worker thread.
        """
        import asyncio as _asyncio  # noqa: PLC0415
        import threading  # noqa: PLC0415
        import time  # noqa: PLC0415

        dispatch_start = threading.Event()
        dispatch_done = threading.Event()

        class _BlockingAdapter:
            name = "blocking"
            should_exit_after_dispatch = True

            def dispatch(self, payload: object) -> None:
                dispatch_start.set()
                # Blocking sleep simulates apprise's sync HTTP call.
                time.sleep(0.05)
                dispatch_done.set()

            def close(self) -> None:
                pass

        spec = _make_spec(poll_seconds=0.0001)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[_BlockingAdapter()],
            budget=Budget(max_runtime_seconds=5.0),
        )

        sentinel_observed: dict[str, bool] = {"saw_dispatch_in_flight": False}

        async def _sentinel() -> None:
            # Wait for the worker thread to enter the blocking sleep, then
            # snapshot whether it is still in flight. With to_thread, the
            # sentinel coroutine reaches this point while dispatch_done is
            # still clear; without it, _dispatch returns synchronously and
            # dispatch_done is already set.
            for _ in range(500):
                if dispatch_start.is_set():
                    break
                await _asyncio.sleep(0)
            sentinel_observed["saw_dispatch_in_flight"] = not dispatch_done.is_set()

        sentinel_task = _asyncio.create_task(_sentinel())
        code = await runner.run()
        await sentinel_task

        assert code == 0
        assert sentinel_observed["saw_dispatch_in_flight"], (
            "sentinel ran only after dispatch completed — _dispatch is blocking the loop"
        )

    async def test_dispatch_offloads_via_to_thread(self) -> None:
        """``_dispatch`` calls each adapter through ``asyncio.to_thread``.

        Direct verification: the adapter's ``dispatch`` method runs on a
        worker thread, not the event-loop thread.
        """
        import threading  # noqa: PLC0415

        loop_thread_id = threading.get_ident()
        captured_thread_ids: list[int] = []

        class _ThreadCheckingAdapter:
            name = "thread-check"
            should_exit_after_dispatch = True

            def dispatch(self, payload: object) -> None:
                captured_thread_ids.append(threading.get_ident())

            def close(self) -> None:
                pass

        spec = _make_spec(poll_seconds=0.0001)
        provider = _make_provider()
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[_ThreadCheckingAdapter()],
            budget=Budget(max_runtime_seconds=5.0),
        )
        code = await runner.run()
        assert code == 0
        assert captured_thread_ids, "dispatch was never called"
        assert all(tid != loop_thread_id for tid in captured_thread_ids), (
            "dispatch ran on the event-loop thread instead of a worker"
        )


class TestRuBannerArithmetic:
    """spec §9.5 — RU/day estimate at startup.

    The banner is informational. The arithmetic is back-of-envelope:
        per-block: 86400 / poll_seconds * 1 RU/read
        per-epoch: poll ticks * (4 epoch-state reads + observable read cost)
        per-event: block ticks * 4 baseline RPCs
    """

    def test_per_block_12s_poll(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # 86400 / 12 = 7200
        assert _estimate_ru_per_day(Cadence.PER_BLOCK, 12.0) == 7_200

    def test_per_block_1s_poll(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        assert _estimate_ru_per_day(Cadence.PER_BLOCK, 1.0) == 86_400

    def test_per_block_60s_poll(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        assert _estimate_ru_per_day(Cadence.PER_BLOCK, 60.0) == 1_440

    def test_per_epoch_includes_state_polling_cost(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # 7,200 polls/day * (4 state reads + 1 observable read).
        assert _estimate_ru_per_day(Cadence.PER_EPOCH, 12.0) == 36_000

    def test_per_epoch_uses_poll_seconds(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        assert _estimate_ru_per_day(Cadence.PER_EPOCH, 1.0) == 432_000
        assert _estimate_ru_per_day(Cadence.PER_EPOCH, 60.0) == 7_200

    def test_per_event_includes_per_block_rpc_baseline(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # 7,200 blocks/day * (events + Timestamp.Now); direct hashes and
        # inferred block numbers do not require per-block lookup RPCs.
        assert _estimate_ru_per_day(Cadence.PER_EVENT, 12.0) == 14_400
        assert _estimate_ru_per_day(Cadence.PER_EVENT, 1.0) == 14_400

    def test_zero_poll_clamps_safely(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # Tiny test-only poll values clamp to seconds-per-day rather than crash.
        assert _estimate_ru_per_day(Cadence.PER_BLOCK, 0.0) == 86_400

    def test_read_cost_multiplies_per_block(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # 86400 / 12 = 7200 ticks/day; with read_cost=3 -> 21,600 RU/day.
        assert _estimate_ru_per_day(Cadence.PER_BLOCK, 12.0, read_cost=3) == 21_600

    def test_read_cost_multiplies_per_epoch(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # 7,200 polls/day * (4 state reads + 4 observable reads).
        assert _estimate_ru_per_day(Cadence.PER_EPOCH, 12.0, read_cost=4) == 57_600

    def test_read_cost_does_not_apply_to_per_event(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # Event subscriptions use their internal per-block RPC baseline.
        assert _estimate_ru_per_day(Cadence.PER_EVENT, 12.0, read_cost=10) == 14_400

    def test_read_cost_default_is_one(self) -> None:
        from chainwake.core.runtime import _estimate_ru_per_day  # noqa: PLC0415

        # Backwards compatibility: omitting read_cost matches the old behaviour.
        assert _estimate_ru_per_day(Cadence.PER_BLOCK, 12.0) == _estimate_ru_per_day(
            Cadence.PER_BLOCK, 12.0, read_cost=1
        )


class TestRuBannerFormat:
    def test_banner_includes_estimate_and_cadence(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_runtime_seconds=None)
        line = _format_ru_banner(spec, Cadence.PER_BLOCK)
        assert line.startswith("Registry-estimated RU: ~7,200/day")
        assert "cadence per_block" in line
        assert "poll 12s" in line
        assert "1 read/tick x 1 RU/read" in line
        assert "excludes bootstrap/retries/SDK RPCs" in line

    def test_banner_unbounded_runtime(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_runtime_seconds=None)
        line = _format_ru_banner(spec, Cadence.PER_BLOCK)
        assert "runtime unbounded" in line

    def test_banner_bounded_runtime(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_runtime_seconds=300.0)
        line = _format_ru_banner(spec, Cadence.PER_BLOCK)
        assert "runtime 300s" in line

    def test_banner_max_ru_unset(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_ru=None)
        line = _format_ru_banner(spec, Cadence.PER_BLOCK)
        assert "max_ru estimate unset" in line

    def test_banner_max_ru_set(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_ru=5_000)
        line = _format_ru_banner(spec, Cadence.PER_BLOCK)
        assert "max_ru estimate 5000" in line

    def test_banner_per_event_says_subscribed(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0)
        line = _format_ru_banner(spec, Cadence.PER_EVENT)
        assert "subscribed" in line
        assert "Registry-estimated RU: ~14,400/day + batched unpins" in line
        assert "legacy fallback ~28,800/day" in line
        assert "2+ baseline RPCs/block" in line

    def test_banner_storage_subscription_is_change_driven(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = TestStorageSubscriptionRuntime._spec()
        line = _format_ru_banner(
            spec,
            Cadence.PER_BLOCK,
            read_cost=2,
            storage_subscription=True,
        )
        assert "change-driven" in line
        assert "~4 RU/setup" in line
        assert "2 RU/baseline" in line
        assert "~4 RU/change" in line

    def test_banner_counts_composite_storage_setup(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = TestStorageSubscriptionRuntime._spec()
        line = _format_ru_banner(
            spec,
            Cadence.PER_BLOCK,
            read_cost=3,
            storage_subscription=True,
            storage_subscription_keys=2,
        )
        assert "subscribed storage (2 keys)" in line
        assert "~5 RU/setup" in line
        assert "~5 RU/change" in line

    def test_banner_new_head_subscription_describes_direct_hashes_and_fallback(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=None)
        line = _format_ru_banner(
            spec,
            Cadence.PER_BLOCK,
            read_cost=3,
            head_subscription=True,
        )
        assert "~21,600/day + batched unpins" in line
        assert "legacy fallback ~28,800/day" in line
        assert "chainHead direct hashes" in line
        assert "0 lookup RPC/block" in line
        assert "3 RU/observable read" in line

    def test_banner_epoch_head_subscription_includes_epoch_state(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=None)
        line = _format_ru_banner(
            spec,
            Cadence.PER_EPOCH,
            read_cost=4,
            epoch_state_read_cost=4,
            head_subscription=True,
        )
        assert "chainHead direct hashes" in line
        assert "4 RU/epoch state" in line
        assert "up to 4 RU/observable read" in line

    def test_banner_reflects_per_entry_read_cost(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_runtime_seconds=None)
        line = _format_ru_banner(spec, Cadence.PER_BLOCK, read_cost=3)
        # 86400 / 12 = 7200 ticks/day; 7200 * 3 = 21,600 RU/day.
        assert "Registry-estimated RU: ~21,600/day" in line
        assert "1 read/tick x 3 RU/read" in line


class TestRuBannerStartup:
    """The banner is written to stderr at WatcherRunner.run() startup."""

    async def test_banner_written_to_provided_stream(self) -> None:
        import io  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_runtime_seconds=300.0)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        entry = lookup("subnet.{netuid}.pool.price")
        banner = io.StringIO()
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
            banner_stream=banner,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        emitted = banner.getvalue()
        assert "Registry-estimated RU:" in emitted
        # Trailing newline so the next line on stderr starts cleanly.
        assert emitted.endswith("\n")

    async def test_banner_written_to_stderr_by_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spec = _make_spec(poll_seconds=12.0)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        entry = lookup("subnet.{netuid}.pool.price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await runner.run()
        captured = capsys.readouterr()
        assert "Registry-estimated RU:" in captured.err
        assert "Registry-estimated RU:" not in captured.out

    async def test_banner_reflects_registry_read_cost_at_runtime(self) -> None:
        """End-to-end: an entry with read_cost > 1 emits the larger RU figure.

        `subnet.{netuid}.pool.depth-for-trade` reads the timestamp, subnet
        existence, and dynamic pool state per tick; the banner must show
        ``1 read/tick x 3 RU/read`` and a
        proportionally larger ``Estimated RU`` value.
        """
        import io  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0, max_runtime_seconds=300.0)
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        entry = lookup("subnet.{netuid}.pool.depth-for-trade")
        assert entry.read_cost == 3  # guard against silent registry drift
        banner = io.StringIO()
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
            banner_stream=banner,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await runner.run()
        emitted = banner.getvalue()
        # 86400 / 12 = 7200 ticks/day; 7200 * 3 = 21,600 RU/day.
        assert "Registry-estimated RU: ~21,600/day" in emitted
        assert "1 read/tick x 3 RU/read" in emitted

    async def test_banner_uses_registry_event_policy(self) -> None:
        import io  # noqa: PLC0415

        spec = _make_spec(poll_seconds=12.0)
        spec.path_params = {}
        spec.primitive_name = "event"
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        # Per-event path takes the subscription route which iterates
        # subscribe_events; mock it to terminate immediately so the banner
        # alone is what the test exercises.
        provider.subscribe_events = MagicMock()

        async def _empty_aiter() -> Any:
            for _ in []:
                yield  # pragma: no cover - never yields

        provider.subscribe_events.return_value = _empty_aiter()
        entry = lookup("event.transfer")
        primitive = _make_primitive(match=False)
        primitive.name = "event"
        banner = io.StringIO()
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=primitive,
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
            banner_stream=banner,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await runner.run()
        emitted = banner.getvalue()
        assert "cadence per_event" in emitted
        assert "subscribed" in emitted
        assert "~14,400/day + batched unpins" in emitted


class TestPerEpochAlignment:
    """Spec §9.3 — per-epoch observables read once per epoch boundary."""

    async def test_per_epoch_loop_evaluates_only_on_epoch_transitions(self) -> None:
        """Evaluation follows chain epoch index, never block modulo arithmetic."""
        observables = [
            _make_observable_at_block(0.5, block=1_000),
            _make_observable_at_block(0.5, block=1_020),
        ]
        spec = _with_delta_window(
            _make_spec(poll_seconds=0.0001, max_runtime_seconds=300.0),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        provider.read_observable = AsyncMock(side_effect=observables)
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(7, block=1_000, last_epoch_block=900),
                _epoch_state(7, block=1_010, last_epoch_block=900),
                _epoch_state(8, block=1_020, last_epoch_block=1_020),
                StopAsyncIteration(),
            ]
        )
        prim = _make_primitive(match=False)
        adapter = _RecordingAdapter(exit_on_dispatch=False)
        entry = lookup("subnet.{netuid}.burn-rate")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=prim,
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
        )
        # Skip the per-block wait so the state watcher iterates immediately.
        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 4

        evaluated_blocks = [call_args.args[0].block for call_args in prim.evaluate.call_args_list]
        assert evaluated_blocks == [1_000, 1_020], (
            f"expected evaluations on chain epoch transitions (1000, 1020); got {evaluated_blocks}"
        )

    async def test_default_per_epoch_schedule_is_driven_by_new_heads(self) -> None:
        class _TwoHeadStream:
            def __init__(self) -> None:
                self._heads = iter(
                    [
                        BlockRef(number=1_010, hash=f"0x{1_010:064x}"),
                        BlockRef(number=1_020, hash=f"0x{1_020:064x}"),
                    ]
                )
                self.closed = False

            def __aiter__(self) -> _TwoHeadStream:
                return self

            async def __anext__(self) -> BlockRef:
                try:
                    return next(self._heads)
                except StopIteration:
                    await asyncio.Event().wait()
                    raise StopAsyncIteration from None

            async def aclose(self) -> None:
                self.closed = True

        baseline = _make_observable_at_block(0.5, block=1_000)
        changed = _make_observable_at_block(0.3, block=1_020)
        stream = _TwoHeadStream()
        spec = _with_delta_window(
            _make_spec(poll_seconds=None, max_runtime_seconds=1.0, target=0.4),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        provider.subscribe_heads = MagicMock(return_value=stream)
        provider.read_observable = AsyncMock(side_effect=[baseline, changed])
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(7, block=1_000, last_epoch_block=900),
                _epoch_state(7, block=1_010, last_epoch_block=900),
                _epoch_state(8, block=1_020, last_epoch_block=1_020),
            ]
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.burn-rate"),
            provider=provider,
            primitive=DeltaPrimitive(
                operator="move-pct",
                target=1,
                window_unit="time",
                window_value="1h",
            ),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=1.0),
        )

        code = await runner.run()

        assert code == 0
        assert stream.closed is True
        assert provider.subscribe_heads.call_count == 1
        assert provider.get_epoch_state.await_args_list[1].args[1] == BlockRef(
            number=1_010,
            hash=f"0x{1_010:064x}",
        )
        assert provider.read_observable.await_count == 2
        assert adapter.received[-1].observed.value == 0.3

    async def test_per_epoch_without_netuid_falls_back_to_per_block_reads(self) -> None:
        """A path with no subnet context must not invent a global epoch."""
        observables = [
            _make_observable_at_block(0.5, block=1_000),
            _make_observable_at_block(0.5, block=1_001),
        ]
        spec = _with_delta_window(
            _make_spec(poll_seconds=0.0001, max_runtime_seconds=300.0),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        provider.epoch_netuid_for.return_value = None
        provider.read_observable = AsyncMock(side_effect=observables)
        prim = _make_primitive(match=False)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.burn-rate"),
            provider=provider,
            primitive=prim,
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=300.0),
        )
        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            await runner.run()
        evaluated_blocks = [call.args[0].block for call in prim.evaluate.call_args_list]
        assert evaluated_blocks == [1_000, 1_001]

    async def test_epoch_window_without_netuid_is_rejected(self) -> None:
        """An epoch-sized window is undefined without one governing subnet."""
        spec = _make_spec()
        spec.condition = DeltaCondition(
            operator="drop-pct",
            target=5,
            window=Window(unit="epochs", value="1"),
        )
        spec.primitive_name = "delta"
        provider = _make_provider()
        provider.epoch_netuid_for.return_value = None
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=_make_primitive(match=False),
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=5),
        )

        with pytest.raises(UserError, match="no single subnet epoch"):
            await runner.run()

    async def test_epoch_window_receives_pinned_epoch_metadata(self) -> None:
        spec = _make_spec(poll_seconds=0.0001, max_runtime_seconds=300)
        spec.condition = DeltaCondition(
            operator="drop-pct",
            target=5,
            window=Window(unit="epochs", value="1"),
        )
        spec.primitive_name = "delta"
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(
            side_effect=[
                _make_observable_at_block(0.5, block=1_000),
                StopAsyncIteration(),
            ]
        )
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(7, block=1_000, last_epoch_block=900),
                StopAsyncIteration(),
            ]
        )
        primitive = _make_primitive(match=False)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=primitive,
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=300),
        )

        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            await runner.run()

        observation = primitive.evaluate.call_args_list[0].args[0]
        assert observation.meta["epoch_index"] == 7
        assert observation.meta["last_epoch_block"] == 900
        assert observation.meta["tempo"] == 99
        assert provider.get_epoch_state.await_args_list[0].args == (
            1,
            BlockRef(number=1_000, hash="0xabc"),
        )

    async def test_epoch_window_head_path_receives_pinned_epoch_metadata(self) -> None:
        class _OneHeadThenSilentStream:
            closed = False
            sent = False

            def __aiter__(self) -> _OneHeadThenSilentStream:
                return self

            async def __anext__(self) -> BlockRef:
                if not self.sent:
                    self.sent = True
                    return BlockRef(number=1_001, hash=f"0x{1_001:064x}")
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        spec = _make_spec(poll_seconds=None, max_runtime_seconds=300)
        spec.condition = DeltaCondition(
            operator="drop-pct",
            target=5,
            window=Window(unit="epochs", value="1"),
        )
        spec.primitive_name = "delta"
        baseline = _make_observable_at_block(0.5, block=1_000)
        changed = _make_observable_at_block(0.4, block=1_001)
        stream = _OneHeadThenSilentStream()
        provider = _make_provider(baseline, cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(side_effect=[baseline, changed])
        provider.subscribe_heads = MagicMock(return_value=stream)
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(7, block=1_000, last_epoch_block=900),
                _epoch_state(8, block=1_001, last_epoch_block=1_001),
            ]
        )
        primitive = _make_primitive(match=False)
        second_evaluation = asyncio.Event()
        evaluation_count = 0

        def evaluate(_observation: ObservableValue) -> NoMatch:
            nonlocal evaluation_count
            evaluation_count += 1
            if evaluation_count == 2:
                second_evaluation.set()
            return NoMatch()

        primitive.evaluate.side_effect = evaluate
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.pool.price"),
            provider=provider,
            primitive=primitive,
            adapters=[_RecordingAdapter()],
            budget=Budget(max_runtime_seconds=300),
        )

        run_task = asyncio.create_task(runner.run())
        await asyncio.wait_for(second_evaluation.wait(), timeout=1)
        runner._shutdown_event.set()
        code = await asyncio.wait_for(run_task, timeout=1)

        assert code == 1
        assert stream.closed is True
        baseline_enriched = primitive.evaluate.call_args_list[0].args[0]
        changed_enriched = primitive.evaluate.call_args_list[1].args[0]
        assert baseline_enriched.meta["epoch_index"] == 7
        assert baseline_enriched.meta["last_epoch_block"] == 900
        assert changed_enriched.meta["epoch_index"] == 8
        assert changed_enriched.meta["last_epoch_block"] == 1_001
        assert provider.get_epoch_state.await_args_list[0].args == (
            1,
            BlockRef(number=1_000, hash="0xabc"),
        )
        assert provider.get_epoch_state.await_args_list[1].args == (
            1,
            BlockRef(number=1_001, hash="0xabc"),
        )

    async def test_per_epoch_loop_dispatches_match_on_priming_read(self) -> None:
        """First read primes last_epoch and fires the primitive once."""
        observables = [
            _make_observable_at_block(0.5, block=361),
            _make_observable_at_block(0.5, block=720),
        ]
        spec = _with_delta_window(
            _make_spec(poll_seconds=0.0001, max_runtime_seconds=300.0),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        provider.read_observable = AsyncMock(side_effect=observables)
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(0, block=10, last_epoch_block=0),
                _epoch_state(0, block=200, last_epoch_block=0),
                StopAsyncIteration(),
            ]
        )
        prim = _make_primitive(match=True, primitive_name="delta")
        adapter = _RecordingAdapter(exit_on_dispatch=True)

        entry = lookup("subnet.{netuid}.burn-rate")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=prim,
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
        )
        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            code = await runner.run()
        assert code == 0
        assert len(adapter.received) == 1
        assert adapter.received[0].status == "matched"

    async def test_per_epoch_loop_skips_within_same_epoch(self) -> None:
        """Repeated state checks in one epoch yield one observable evaluation."""
        observables = [_make_observable_at_block(0.5, block=10)]
        spec = _with_delta_window(
            _make_spec(poll_seconds=0.0001, max_runtime_seconds=300.0),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        provider.read_observable = AsyncMock(side_effect=observables)
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(0, block=10, last_epoch_block=0),
                _epoch_state(0, block=200, last_epoch_block=0),
                StopAsyncIteration(),
            ]
        )
        prim = _make_primitive(match=False)
        adapter = _RecordingAdapter(exit_on_dispatch=False)
        entry = lookup("subnet.{netuid}.burn-rate")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=prim,
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
        )
        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            await runner.run()

        # The priming state reads once; block 200 has the same chain marker.
        evaluated_blocks = [call_args.args[0].block for call_args in prim.evaluate.call_args_list]
        assert evaluated_blocks == [10], (
            f"expected single evaluation at priming read; got {evaluated_blocks}"
        )

    async def test_rate_limit_does_not_consume_epoch_marker(self) -> None:
        """A failed observable read must retry the same chain epoch."""
        spec = _with_delta_window(
            _make_spec(poll_seconds=0.0001, max_runtime_seconds=300.0),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_EPOCH)
        provider.get_epoch_state = AsyncMock(
            side_effect=[
                _epoch_state(7, block=1_000, last_epoch_block=900),
                _epoch_state(7, block=1_000, last_epoch_block=900),
            ]
        )
        provider.read_observable = AsyncMock(
            side_effect=[
                RateLimitError("free-tier limit"),
                _make_observable_at_block(0.5, block=1_000),
            ]
        )
        primitive = _make_primitive(match=True, primitive_name="delta")
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        runner = WatcherRunner(
            spec,
            entry=lookup("subnet.{netuid}.burn-rate"),
            provider=provider,
            primitive=primitive,
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
        )

        with (
            patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock),
            patch("chainwake.core.retry.asyncio.sleep", new_callable=AsyncMock),
        ):
            code = await runner.run()

        assert code == 0
        assert provider.read_observable.await_count == 2
        primitive.evaluate.assert_called_once()

    async def test_per_block_loop_unchanged_for_per_block_cadence(self) -> None:
        """PER_BLOCK cadence still evaluates every read, no epoch gating."""
        observables = [
            _make_observable_at_block(0.5, block=10),
            _make_observable_at_block(0.5, block=11),
            _make_observable_at_block(0.5, block=12),
        ]
        spec = _with_delta_window(
            _make_spec(poll_seconds=0.0001, max_runtime_seconds=300.0),
            unit="time",
            value="1h",
        )
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        provider.read_observable = AsyncMock(side_effect=observables)
        prim = _make_primitive(match=False)
        adapter = _RecordingAdapter(exit_on_dispatch=False)
        entry = lookup("subnet.{netuid}.pool.moving-price")
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=prim,
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=300.0),
        )
        with patch.object(WatcherRunner, "_interruptible_sleep", new_callable=AsyncMock):
            await runner.run()

        # Every read triggers an evaluate — no epoch gating in the per-block loop.
        evaluated_blocks = [call_args.args[0].block for call_args in prim.evaluate.call_args_list]
        assert evaluated_blocks == [10, 11, 12], (
            f"per-block loop must evaluate every read; got {evaluated_blocks}"
        )


class TestEffectivePollResolution:
    """Bug 2 — ``--poll-seconds`` defaults to the chain's natural cadence."""

    def test_explicit_poll_overrides_cadence_default(self) -> None:
        from chainwake.core.runtime import _resolve_effective_poll  # noqa: PLC0415

        # Explicit user override always wins.
        assert _resolve_effective_poll(5.0) == 5.0
        assert _resolve_effective_poll(0.5) == 0.5

    def test_none_poll_falls_back_to_block_time(self) -> None:
        from chainwake.core.runtime import _resolve_effective_poll  # noqa: PLC0415

        # Bittensor mainnet block time is 12s; the runtime ships that as the
        # fallback when the user doesn't pass ``--poll-seconds``. The helper
        # is cadence-agnostic. Per-epoch watchers use it to inspect the
        # stateful epoch marker; event subscriptions ignore it.
        assert _resolve_effective_poll(None) == 12.0

    def test_zero_poll_treated_as_explicit_override(self) -> None:
        from chainwake.core.runtime import _resolve_effective_poll  # noqa: PLC0415

        # ``0.0`` means "tight loop, no sleep" — only used by tests. The
        # resolver must not silently upgrade it to the cadence default.
        assert _resolve_effective_poll(0.0) == 0.0

    def test_banner_uses_resolved_poll_when_spec_unset(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="below", target=0.5),
            invocation=["chainwake"],
            poll_seconds=None,
            max_runtime_seconds=300.0,
        )
        line = _format_ru_banner(spec, Cadence.PER_BLOCK)
        # 86400 / 12 = 7200 RU/day; banner reports the resolved 12s.
        assert "Registry-estimated RU: ~7,200/day" in line
        assert "poll 12s" in line

    def test_banner_explicit_effective_poll_wins(self) -> None:
        from chainwake.core.runtime import _format_ru_banner  # noqa: PLC0415

        spec = WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="below", target=0.5),
            invocation=["chainwake"],
            poll_seconds=None,
            max_runtime_seconds=300.0,
        )
        # Caller supplies the runtime-resolved value; the banner uses it
        # verbatim instead of recomputing from the spec.
        line = _format_ru_banner(spec, Cadence.PER_BLOCK, effective_poll=6.0)
        assert "poll 6s" in line
        assert "Registry-estimated RU: ~14,400/day" in line  # 86400 / 6 = 14400

    async def test_runner_emits_banner_with_resolved_block_time(self) -> None:
        """Omitting poll on a PER_BLOCK observable selects new-head scheduling."""
        import io  # noqa: PLC0415

        spec = WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=ThresholdCondition(operator="below", target=0.5),
            invocation=["chainwake"],
            poll_seconds=None,
            max_runtime_seconds=None,
        )
        adapter = _RecordingAdapter(exit_on_dispatch=True)
        provider = _make_provider(cadence=Cadence.PER_BLOCK)
        entry = lookup("subnet.{netuid}.pool.moving-price")
        banner = io.StringIO()
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=_make_primitive(match=True),
            adapters=[adapter],
            budget=Budget(max_runtime_seconds=5.0),
            banner_stream=banner,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await runner.run()
        emitted = banner.getvalue()
        assert "chainHead direct hashes" in emitted
        assert "0 lookup RPC/block" in emitted
        assert "runtime unbounded" in emitted


class TestHeartbeat:
    """In-place stderr heartbeat after each successful poll, TTY-only."""

    def test_emit_heartbeat_writes_to_tty_stream(self) -> None:
        import io  # noqa: PLC0415

        from chainwake.core.runtime import _emit_heartbeat  # noqa: PLC0415

        class _FakeTTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = _FakeTTY()
        _emit_heartbeat(5234567, 24, stream=stream)
        out = stream.getvalue()
        # Carriage return + content; no newline so the next tick overwrites.
        assert out.startswith("\r")
        assert "5234567" in out
        assert "24 RPCs" in out
        assert "\n" not in out

    def test_emit_heartbeat_includes_value_when_provided(self) -> None:
        import io  # noqa: PLC0415

        from chainwake.core.runtime import _emit_heartbeat  # noqa: PLC0415

        class _FakeTTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = _FakeTTY()
        # High-precision float so users can see volatility at the 10th
        # decimal place — important for stable subnet prices that drift
        # only in the deepest fraction.
        _emit_heartbeat(100, 5, 0.0137088763, stream=stream)
        out = stream.getvalue()
        assert "value=0.0137088763" in out

    def test_format_observed_value_handles_common_types(self) -> None:
        from chainwake.core.runtime import _format_observed_value  # noqa: PLC0415

        assert _format_observed_value(0.0137088763) == "0.0137088763"
        assert _format_observed_value(42) == "42"
        assert _format_observed_value(True) == "True"
        # Long strings (e.g. SS58 addresses) are truncated so the
        # heartbeat line doesn't wrap and break \r overwrite.
        long = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        formatted = _format_observed_value(long)
        assert len(formatted) <= 24
        assert formatted.endswith("...")

    def test_emit_heartbeat_silent_on_non_tty(self) -> None:
        import io  # noqa: PLC0415

        from chainwake.core.runtime import _emit_heartbeat  # noqa: PLC0415

        class _FakePipe(io.StringIO):
            def isatty(self) -> bool:
                return False

        stream = _FakePipe()
        _emit_heartbeat(5234567, 24, stream=stream)
        # Redirected stderr (CI logs, file capture) must stay clean — the \r
        # trick is meaningless without a terminal and would just spam.
        assert stream.getvalue() == ""

    def test_clear_heartbeat_blanks_the_line(self) -> None:
        import io  # noqa: PLC0415

        from chainwake.core.runtime import _clear_heartbeat  # noqa: PLC0415

        class _FakeTTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = _FakeTTY()
        _clear_heartbeat(stream=stream)
        out = stream.getvalue()
        # \r + spaces + \r so the previous heartbeat content is overwritten
        # before the final payload prints on its own line.
        assert out.startswith("\r")
        assert out.endswith("\r")
        assert " " in out

    def test_clear_heartbeat_silent_on_non_tty(self) -> None:
        import io  # noqa: PLC0415

        from chainwake.core.runtime import _clear_heartbeat  # noqa: PLC0415

        class _FakePipe(io.StringIO):
            def isatty(self) -> bool:
                return False

        stream = _FakePipe()
        _clear_heartbeat(stream=stream)
        assert stream.getvalue() == ""
