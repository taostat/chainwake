"""Unit tests for BittensorProvider helpers and dispatch logic.

These tests exercise pure Python logic (path_template conversion, cadence
lookups, event decoding, computed observable math) without touching a real
RPC node.  Tests that require a live node live in
tests/integration/providers/test_bittensor_provider.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import blake2b
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from async_substrate_interface import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException

from chainwake.core.errors import (
    AuthError,
    DecodeError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
    TxNotFoundInHorizonError,
    UserError,
)
from chainwake.core.registry import ObservationDriver, all_entries, lookup
from chainwake.providers.base import (
    BlockRef,
    Cadence,
    Event,
    EventFilter,
    ProviderConfig,
    TxFinalityStatus,
)
from chainwake.providers.bittensor import (
    _FRIENDLY_TO_SUBSTRATE,
    _TX_SEARCH_HORIZON_BLOCKS,
    RAO_PER_TAO,
    BittensorProvider,
    _decode_identity_value,
    _EventSubscription,
    _HeadSubscription,
    _path_template,
    _rao_to_tao,
    _scale_to_int,
    _StorageSubscription,
    _wrap_substrate_exception,
)
from chainwake.providers.market import MarketPriceFeed, MarketPriceSource, UsdPrice
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _scale_to_int
# ---------------------------------------------------------------------------


def test_scale_to_int_none():
    assert _scale_to_int(None) == 0


def test_scale_to_int_plain_int():
    assert _scale_to_int(42) == 42


def test_scale_to_int_dict_returns_zero():
    assert _scale_to_int({"bits": 0}) == 0


class _ScaleType:
    def __init__(self, v: object) -> None:
        self.value = v


def test_scale_to_int_scale_type():
    assert _scale_to_int(_ScaleType(1234)) == 1234


def test_scale_to_int_zero_string():
    assert _scale_to_int(_ScaleType("0")) == 0


def test_decode_identity_value_preserves_non_utf8_bytes_as_hex() -> None:
    """Vec<u8> identity data is bytes, not guaranteed UTF-8 text."""
    assert _decode_identity_value("0xff") == "0xff"
    assert _decode_identity_value(b"\xff\x00") == "0xff00"


def test_decode_identity_value_handles_nested_scale_shapes_losslessly() -> None:
    """Scale decoders may wrap identity bytes in Raw variants and containers."""
    assert _decode_identity_value({"Raw": "0x416c696365"}) == "Alice"
    assert _decode_identity_value({"Raw": {"Raw": "0xff"}}) == "0xff"
    assert _decode_identity_value(["0x6f6e65", {"Raw": "0xff"}]) == ["one", "0xff"]


class _CurrentPruningSubstrate:
    """Minimal spec-440 shape proving the former pruning reads are not valid."""

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        assert module == "SubtensorModule"
        if storage_fn == "Uids":
            return _ScaleType(0)
        if storage_fn == "PruningScores":
            raise SubstrateRequestException(
                'Storage function "SubtensorModule.PruningScores" not found'
            )
        values: dict[str, object] = {
            "LastUpdate": [100],
            "ActivityCutoff": 5_000,
            "ImmunityPeriod": 100,
            "BlockAtRegistration": 0,
        }
        return _ScaleType(values[storage_fn])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "parts"),
    [
        (
            "neuron.{netuid}.{hotkey}.pruning-score",
            ["neuron", "1", "5FakeHotkey"],
        ),
        (
            "neuron.{netuid}.{hotkey}.blocks-until-deregistration",
            ["neuron", "1", "5FakeHotkey"],
        ),
    ],
)
async def test_removed_pruning_observables_are_not_dispatched(
    template: str,
    parts: list[str],
) -> None:
    """Spec 440 removed PruningScores and has no deterministic deregistration block."""
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _CurrentPruningSubstrate())

    with pytest.raises(NotImplementedError, match="no neuron handler"):
        await provider._dispatch_neuron(template, parts, {}, 200, "0xabc")


# ---------------------------------------------------------------------------
# _rao_to_tao
# ---------------------------------------------------------------------------


def test_rao_to_tao_whole_number():
    assert _rao_to_tao(RAO_PER_TAO) == 1.0


def test_rao_to_tao_zero():
    assert _rao_to_tao(0) == 0.0


def test_rao_to_tao_half():
    assert _rao_to_tao(RAO_PER_TAO // 2) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _path_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("subnet.19.pool.price", "subnet.{netuid}.pool.price"),
        ("subnet.1.pool.tao-depth", "subnet.{netuid}.pool.tao-depth"),
        (
            "neuron.19.5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY.incentive",
            "neuron.{netuid}.{hotkey}.incentive",
        ),
        (
            "validator.5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY.commission",
            "validator.{hotkey}.commission",
        ),
        (
            "account.5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY.balance",
            "account.{coldkey}.balance",
        ),
        ("network.subnet-registration-cost", "network.subnet-registration-cost"),
        ("network.runtime-version", "network.runtime-version"),
        (
            "tx.0xabc123def456abc123def456abc123de",
            "tx.{tx_hash}",
        ),
    ],
)
def test_path_template(path: str, expected: str) -> None:
    assert _path_template(path) == expected


def test_path_template_literal_passthrough():
    assert _path_template("network.subnet-count") == "network.subnet-count"


# ---------------------------------------------------------------------------
# natural_cadence_for
# ---------------------------------------------------------------------------


def test_natural_cadence_subnet_price():
    p = BittensorProvider()
    assert p.natural_cadence_for("subnet.19.pool.price") == Cadence.PER_BLOCK


def test_natural_cadence_registration_cost():
    p = BittensorProvider()
    assert p.natural_cadence_for("subnet.19.registration-cost") == Cadence.PER_BLOCK


def test_natural_cadence_event_type():
    p = BittensorProvider()
    assert p.natural_cadence_for("network.--on-runtime-upgraded") == Cadence.PER_EVENT


def test_natural_cadence_generic_event_type():
    p = BittensorProvider()
    assert p.natural_cadence_for("event.transfer") == Cadence.PER_EVENT


def test_natural_cadence_neuron_incentive():
    p = BittensorProvider()
    assert (
        p.natural_cadence_for(
            "neuron.19.5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY.incentive"
        )
        == Cadence.PER_EPOCH
    )


def test_natural_cadence_unknown_defaults_to_per_block():
    p = BittensorProvider()
    assert p.natural_cadence_for("some.unknown.path") == Cadence.PER_BLOCK


def test_natural_cadence_tx():
    p = BittensorProvider()
    assert p.natural_cadence_for("tx.0xabc123def456abc123def456abc123de") == Cadence.OTHER


# ---------------------------------------------------------------------------
# Registry cadence completeness
# ---------------------------------------------------------------------------


def test_path_cadence_covers_all_appendix_a_templates():
    """Every registry entry owns a known cadence value."""
    for entry in all_entries():
        assert entry.natural_cadence in Cadence


# ---------------------------------------------------------------------------
# _FRIENDLY_TO_SUBSTRATE completeness (Appendix B)
# ---------------------------------------------------------------------------

APPENDIX_B_FRIENDLY_NAMES = [
    "transfer",
    "stake-added",
    "stake-removed",
    "swap",
    "neuron-registered",
    "subnet-registered",
    "weights-set",
    "axon-served",
    "validator-permit-changed",
    "child-keys-set",
    "identity-set",
]


def test_all_appendix_b_friendly_names_present():
    for name in APPENDIX_B_FRIENDLY_NAMES:
        assert name in _FRIENDLY_TO_SUBSTRATE, f"Missing friendly event name: {name!r}"


def test_unobservable_friendly_events_are_not_advertised():
    assert "neuron-deregistered" not in _FRIENDLY_TO_SUBSTRATE
    assert "hyperparam-changed" not in _FRIENDLY_TO_SUBSTRATE


def test_friendly_to_substrate_all_nonempty():
    for name, substrates in _FRIENDLY_TO_SUBSTRATE.items():
        assert len(substrates) >= 1, f"{name!r} maps to empty substrate list"


def test_swap_maps_to_stake_swapped():
    # spec-401: SwapTaoForAlpha/SwapAlphaForTao do not exist; StakeSwapped is the
    # unified event emitted for all alpha-TAO swap operations.
    substrates = _FRIENDLY_TO_SUBSTRATE["swap"]
    assert "SubtensorModule.StakeSwapped" in substrates


# ---------------------------------------------------------------------------
# Event decoding (via _EventSubscription._decode_event)
# ---------------------------------------------------------------------------


def _decode(
    raw: dict[str, object],
    event_filter: EventFilter,
) -> Event | None:
    # _decode_event is pure Python and never calls the substrate; cast to
    # satisfy the type checker without importing the real implementation.
    stub = cast(AsyncSubstrateInterface, object())
    sub = _EventSubscription(stub, event_filter)
    return sub._decode_event(raw, 100, "0xabc", datetime.now(UTC))


def test_decode_event_friendly_match():
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "NetworkAdded",
        "attributes": {"netuid": 5},
    }
    filt = EventFilter(event_types=("subnet-registered",))
    ev = _decode(raw, filt)
    assert ev is not None
    assert ev.event_type == "subnet-registered"
    assert ev.raw_event == "SubtensorModule.NetworkAdded"
    assert ev.args["netuid"] == 5


def test_decode_event_raw_match():
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "NetworkAdded",
        "attributes": {},
    }
    filt = EventFilter(event_types=("SubtensorModule.NetworkAdded",))
    ev = _decode(raw, filt)
    assert ev is not None
    assert ev.raw_event == "SubtensorModule.NetworkAdded"


def test_decode_event_no_match_drops():
    raw: dict[str, object] = {
        "module_id": "System",
        "event_id": "ExtrinsicSuccess",
        "attributes": {},
    }
    filt = EventFilter(event_types=("subnet-registered",))
    ev = _decode(raw, filt)
    assert ev is None


def test_decode_event_args_match_filters():
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "NetworkAdded",
        "attributes": {"netuid": 5},
    }
    filt = EventFilter(event_types=("subnet-registered",), args_match={"netuid": 7})
    ev = _decode(raw, filt)
    assert ev is None  # netuid=5 != 7


def test_decode_event_args_match_passes():
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "NetworkAdded",
        "attributes": {"netuid": 7},
    }
    filt = EventFilter(event_types=("subnet-registered",), args_match={"netuid": 7})
    ev = _decode(raw, filt)
    assert ev is not None
    assert ev.args["netuid"] == 7


def test_decode_swap_stake_swapped():
    # spec-401 emits StakeSwapped for all alpha-TAO swap operations.
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "StakeSwapped",
        "attributes": {"netuid": 1, "amount": 1000},
    }
    filt = EventFilter(event_types=("swap",))
    ev = _decode(raw, filt)
    assert ev is not None
    assert ev.event_type == "swap"


def test_decode_event_non_dict_attributes():
    raw: dict[str, object] = {
        "module_id": "Balances",
        "event_id": "Transfer",
        "attributes": "not-a-dict",
    }
    filt = EventFilter(event_types=("transfer",))
    ev = _decode(raw, filt)
    assert ev is not None
    assert ev.args == {}


def test_decode_event_preserves_positional_attributes_without_metadata_names():
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "WeightsSet",
        "attributes": (11, 74),
    }
    filt = EventFilter(event_types=("weights-set",))

    ev = _decode(raw, filt)

    assert ev is not None
    assert ev.args == {"arg_0": 11, "arg_1": 74}


def test_decode_event_uses_metadata_names_for_positional_attributes():
    raw: dict[str, object] = {
        "module_id": "Balances",
        "event_id": "Transfer",
        "attributes": ("5Alice", "5Bob", 2_000),
    }
    filt = EventFilter(
        event_types=("transfer",),
        args_match={"from": "5Alice", "to": "5Bob"},
        amount_min=1_000,
    )
    stub = cast(AsyncSubstrateInterface, object())
    sub = _EventSubscription(stub, filt)

    ev = sub._decode_event(
        raw,
        100,
        "0xabc",
        datetime.now(UTC),
        argument_names=("from", "to", "amount"),
    )

    assert ev is not None
    assert ev.args == {"from": "5Alice", "to": "5Bob", "amount": 2_000}


def test_event_subscription_queue_is_bounded_for_backpressure():
    stub = cast(AsyncSubstrateInterface, object())
    sub = _EventSubscription(stub, EventFilter(event_types=("transfer",)))

    assert sub._queue.maxsize > 0


async def test_head_subscription_prefers_chainhead_direct_hash_and_meters_setup() -> None:
    class _ChainHeadSubstrate:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        async def rpc_request(
            self,
            method: str,
            params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            self.calls.append((method, params))
            if method == "chainHead_v1_follow":
                handler = cast(Any, result_handler)
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "initialized",
                                "finalizedBlockHashes": ["0xfinal"],
                            }
                        }
                    },
                    "follow-1",
                )
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "newBlock",
                                "blockHash": "0xabc",
                                "parentBlockHash": "0xfinal",
                            }
                        }
                    },
                    "follow-1",
                )
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "bestBlockChanged",
                                "bestBlockHash": "0xabc",
                            }
                        }
                    },
                    "follow-1",
                )
                await asyncio.Event().wait()
            return {"result": None}

        async def get_block_header(self, block_hash: str) -> dict[str, object]:
            assert block_hash == "0xfinal"
            return {"header": {"number": 122}}

        async def subscribe_block_headers(self, _handler: object) -> None:
            raise AssertionError("legacy head subscription should not be used")

    charges: list[int] = []
    fake = _ChainHeadSubstrate()
    substrate = cast(AsyncSubstrateInterface, fake)
    subscription = _HeadSubscription(substrate, charge_rpc=charges.append)

    block = await asyncio.wait_for(anext(subscription), timeout=0.2)

    assert block == BlockRef(number=123, hash="0xabc")
    assert charges == [1, 1]
    await subscription.aclose()
    assert ("chainHead_v1_follow", [False]) in fake.calls
    assert ("chainHead_v1_unfollow", ["follow-1"]) in fake.calls


async def test_head_subscription_unpins_old_initialized_blocks_and_surfaces_stop() -> None:
    class _StoppedChainHeadSubstrate:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        async def rpc_request(
            self,
            method: str,
            params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            self.calls.append((method, params))
            if method == "chainHead_v1_follow":
                handler = cast(Any, result_handler)
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "initialized",
                                "finalizedBlockHashes": ["0xold", "0xfinal"],
                            }
                        }
                    },
                    "follow-1",
                )
                await handler(
                    {"params": {"result": {"event": "stop"}}},
                    "follow-1",
                )
            return {"result": None}

        async def get_block_header(self, block_hash: str) -> dict[str, object]:
            assert block_hash == "0xfinal"
            return {"header": {"number": "0x7a"}}

    charges: list[int] = []
    fake = _StoppedChainHeadSubstrate()
    subscription = _HeadSubscription(
        cast(AsyncSubstrateInterface, fake),
        charge_rpc=charges.append,
    )

    with pytest.raises(SubscriptionFailedError, match="follow stopped"):
        await anext(subscription)

    await subscription.aclose()
    assert charges == [1, 1, 1]
    assert ("chainHead_v1_unpin", ["follow-1", ["0xold"]]) in fake.calls
    assert not any(method == "chainHead_v1_unfollow" for method, _params in fake.calls)


async def test_head_subscription_batches_finalized_and_pruned_unpins() -> None:
    class _UnpinSubstrate:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        async def rpc_request(
            self,
            method: str,
            params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            del result_handler
            self.calls.append((method, params))
            return {"result": None}

    fake = _UnpinSubstrate()
    charges: list[int] = []
    subscription = _HeadSubscription(
        cast(AsyncSubstrateInterface, fake),
        charge_rpc=charges.append,
    )
    pruned = [f"0xpruned-{index}" for index in range(31)]
    subscription._follow_subscription = "follow-1"
    subscription._finalized_hash = "0xfinal-0"
    subscription._pinned.update(["0xfinal-0", "0xfinal-1", *pruned])

    await subscription._record_chainhead_finality(
        {
            "finalizedBlockHashes": ["0xfinal-1"],
            "prunedBlockHashes": pruned,
        }
    )

    assert charges == [1]
    assert len(fake.calls) == 1
    method, params = fake.calls[0]
    assert method == "chainHead_v1_unpin"
    assert params[0] == "follow-1"
    assert set(cast(list[str], params[1])) == {"0xfinal-0", *pruned}


async def test_head_subscription_falls_back_when_chainhead_is_unsupported() -> None:
    class _LegacyOnlySubstrate:
        async def rpc_request(
            self,
            method: str,
            _params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            del result_handler
            assert method == "chainHead_v1_follow"
            raise SubstrateRequestException("Method not found")

        async def subscribe_block_headers(self, handler: object) -> None:
            await cast(Any, handler)({"header": {"number": 123}})
            await asyncio.Event().wait()

        async def get_block_hash(self, block: int) -> str:
            assert block == 123
            return "0xabc"

    charges: list[int] = []
    substrate = cast(AsyncSubstrateInterface, _LegacyOnlySubstrate())
    subscription = _HeadSubscription(substrate, charge_rpc=charges.append)

    block = await asyncio.wait_for(anext(subscription), timeout=0.2)

    assert block == BlockRef(number=123, hash="0xabc")
    assert charges == [1, 1, 1]
    await subscription.aclose()


async def test_head_subscription_surfaces_failure() -> None:
    class _FailedHeadSubstrate:
        async def rpc_request(
            self,
            _method: str,
            _params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            del result_handler
            raise RuntimeError("head subscription exploded")

    substrate = cast(AsyncSubstrateInterface, _FailedHeadSubstrate())
    subscription = _HeadSubscription(substrate)

    with pytest.raises(SubscriptionFailedError, match="head subscription exploded"):
        await anext(subscription)

    await subscription.aclose()


async def test_event_subscription_surfaces_subscription_failure() -> None:
    class _FailingSubstrate:
        async def rpc_request(
            self,
            _method: str,
            _params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            del result_handler
            raise RuntimeError("subscription exploded")

    substrate = cast(AsyncSubstrateInterface, _FailingSubstrate())
    subscription = _EventSubscription(substrate, EventFilter(event_types=("transfer",)))

    with pytest.raises(SubscriptionFailedError, match="subscription exploded"):
        await anext(subscription)

    await subscription.aclose()


async def test_event_subscription_uses_chain_timestamp_and_meters_every_rpc() -> None:
    timestamp_ms = 1_700_000_000_123

    class _OneBlockSubstrate:
        async def rpc_request(
            self,
            method: str,
            _params: list[object],
            result_handler: object | None = None,
        ) -> dict[str, object]:
            if method == "chainHead_v1_follow":
                handler = cast(Any, result_handler)
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "initialized",
                                "finalizedBlockHashes": ["0xfinal"],
                            }
                        }
                    },
                    "follow-1",
                )
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "newBlock",
                                "blockHash": "0xabc",
                                "parentBlockHash": "0xfinal",
                            }
                        }
                    },
                    "follow-1",
                )
                await handler(
                    {
                        "params": {
                            "result": {
                                "event": "bestBlockChanged",
                                "bestBlockHash": "0xabc",
                            }
                        }
                    },
                    "follow-1",
                )
                await asyncio.Event().wait()
            return {"result": None}

        async def get_block_header(self, block_hash: str) -> dict[str, object]:
            assert block_hash == "0xfinal"
            return {"header": {"number": 99}}

        async def get_events(self, *, block_hash: str) -> list[dict[str, object]]:
            assert block_hash == "0xabc"
            return [
                {
                    "module_id": "Balances",
                    "event_id": "Transfer",
                    "attributes": ("5Alice", "5Bob", 2_000),
                }
            ]

        async def query(
            self,
            module: str,
            storage_fn: str,
            *,
            block_hash: str,
        ) -> _ScaleType:
            assert (module, storage_fn, block_hash) == ("Timestamp", "Now", "0xabc")
            return _ScaleType(timestamp_ms)

        async def get_metadata_event(
            self,
            module_id: str,
            event_id: str,
            block_hash: str,
        ) -> dict[str, object]:
            assert (module_id, event_id, block_hash) == ("Balances", "Transfer", "0xabc")
            return {"fields": [{"name": "from"}, {"name": "to"}, {"name": "amount"}]}

    charges: list[int] = []
    substrate = cast(AsyncSubstrateInterface, _OneBlockSubstrate())
    subscription = _EventSubscription(
        substrate,
        EventFilter(event_types=("transfer",)),
        charge_rpc=charges.append,
    )

    event = await asyncio.wait_for(anext(subscription), timeout=0.2)

    assert event.block == 100
    assert event.block_hash == "0xabc"
    assert event.timestamp == datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    # Follow setup, one finalized-height bootstrap, events, Timestamp.Now,
    # and the first uncached metadata lookup are five actual RPCs. Later
    # blocks need only events + timestamp (plus amortized batched unpins).
    assert charges == [1, 1, 1, 1, 1]
    await subscription.aclose()


# ---------------------------------------------------------------------------
# Event predicate filters: amount_min, direction
# ---------------------------------------------------------------------------


def _transfer_raw(
    *, frm: str = "5Alice", to: str = "5Bob", amount: int = 1000
) -> dict[str, object]:
    return {
        "module_id": "Balances",
        "event_id": "Transfer",
        "attributes": {"from": frm, "to": to, "amount": amount},
    }


def test_decode_event_amount_min_above_threshold_passes():
    raw = _transfer_raw(amount=2_000)
    filt = EventFilter(event_types=("transfer",), amount_min=1_000)
    ev = _decode(raw, filt)
    assert ev is not None
    assert ev.args["amount"] == 2_000


def test_decode_event_amount_min_at_threshold_passes():
    raw = _transfer_raw(amount=1_000)
    filt = EventFilter(event_types=("transfer",), amount_min=1_000)
    assert _decode(raw, filt) is not None


def test_decode_event_amount_min_below_threshold_drops():
    raw = _transfer_raw(amount=500)
    filt = EventFilter(event_types=("transfer",), amount_min=1_000)
    assert _decode(raw, filt) is None


def test_decode_event_amount_min_uses_value_fallback():
    raw: dict[str, object] = {
        "module_id": "Balances",
        "event_id": "Transfer",
        "attributes": {"from": "5A", "to": "5B", "value": 5_000},
    }
    filt = EventFilter(event_types=("transfer",), amount_min=1_000)
    assert _decode(raw, filt) is not None


def test_decode_event_amount_min_drops_when_field_missing():
    raw: dict[str, object] = {
        "module_id": "SubtensorModule",
        "event_id": "NetworkAdded",
        "attributes": {"netuid": 5},
    }
    filt = EventFilter(event_types=("subnet-registered",), amount_min=1)
    assert _decode(raw, filt) is None


def test_decode_event_direction_in_matches_to_address():
    raw = _transfer_raw(frm="5Alice", to="5Bob")
    filt = EventFilter(
        event_types=("transfer",),
        direction="in",
        direction_address="5Bob",
    )
    assert _decode(raw, filt) is not None


def test_decode_event_direction_in_drops_other_recipient():
    raw = _transfer_raw(frm="5Alice", to="5Carol")
    filt = EventFilter(
        event_types=("transfer",),
        direction="in",
        direction_address="5Bob",
    )
    assert _decode(raw, filt) is None


def test_decode_event_direction_out_matches_from_address():
    raw = _transfer_raw(frm="5Alice", to="5Bob")
    filt = EventFilter(
        event_types=("transfer",),
        direction="out",
        direction_address="5Alice",
    )
    assert _decode(raw, filt) is not None


def test_decode_event_direction_out_drops_other_sender():
    raw = _transfer_raw(frm="5Carol", to="5Bob")
    filt = EventFilter(
        event_types=("transfer",),
        direction="out",
        direction_address="5Alice",
    )
    assert _decode(raw, filt) is None


def test_decode_event_direction_both_keeps_event():
    raw = _transfer_raw(frm="5Alice", to="5Bob")
    filt = EventFilter(
        event_types=("transfer",),
        direction="both",
        direction_address="5Anyone",
    )
    assert _decode(raw, filt) is not None


def test_decode_event_amount_min_and_direction_combined_passes():
    raw = _transfer_raw(frm="5Alice", to="5Bob", amount=10_000)
    filt = EventFilter(
        event_types=("transfer",),
        amount_min=5_000,
        direction="in",
        direction_address="5Bob",
    )
    assert _decode(raw, filt) is not None


def test_decode_event_amount_min_and_direction_combined_drops_on_amount():
    raw = _transfer_raw(frm="5Alice", to="5Bob", amount=100)
    filt = EventFilter(
        event_types=("transfer",),
        amount_min=5_000,
        direction="in",
        direction_address="5Bob",
    )
    assert _decode(raw, filt) is None


def test_decode_event_amount_min_and_direction_combined_drops_on_direction():
    raw = _transfer_raw(frm="5Alice", to="5Carol", amount=10_000)
    filt = EventFilter(
        event_types=("transfer",),
        amount_min=5_000,
        direction="in",
        direction_address="5Bob",
    )
    assert _decode(raw, filt) is None


def test_decode_event_args_match_and_amount_min_combined():
    raw = _transfer_raw(frm="5Alice", to="5Bob", amount=10_000)
    filt = EventFilter(
        event_types=("transfer",),
        args_match={"from": "5Alice"},
        amount_min=5_000,
    )
    assert _decode(raw, filt) is not None


# ---------------------------------------------------------------------------
# Provider connection guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_raises_if_not_connected():
    p = BittensorProvider()
    with pytest.raises(RuntimeError, match="connect"):
        await p.read_observable("subnet.1.pool.price", {})


@pytest.mark.asyncio
async def test_provider_raises_value_error_for_empty_rpc_url():
    p = BittensorProvider()
    with pytest.raises(ValueError, match="rpc_url"):
        await p.connect(ProviderConfig(rpc_url=""))


@pytest.mark.asyncio
async def test_connect_forwards_api_key_as_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``api_key`` on ProviderConfig is set as Authorization on the WS handshake."""
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    fake_ws = MagicMock()
    fake_ws._options = {"max_size": 1, "write_limit": 1}
    fake_substrate = MagicMock()
    fake_substrate.ws = fake_ws
    fake_substrate.initialize = AsyncMock()

    def _factory(*_args: object, **_kwargs: object) -> object:
        return fake_substrate

    monkeypatch.setattr(
        "chainwake.providers.bittensor.AsyncSubstrateInterface",
        _factory,
    )

    p = BittensorProvider()
    await p.connect(ProviderConfig(rpc_url="ws://localhost", api_key="secret-token"))

    headers = fake_ws._options.get("additional_headers")
    assert headers == {"Authorization": "Bearer secret-token"}


@pytest.mark.asyncio
async def test_connect_without_api_key_does_not_set_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No api_key → no Authorization header touch."""
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    fake_ws = MagicMock()
    fake_ws._options = {"max_size": 1, "write_limit": 1}
    fake_substrate = MagicMock()
    fake_substrate.ws = fake_ws
    fake_substrate.initialize = AsyncMock()

    monkeypatch.setattr(
        "chainwake.providers.bittensor.AsyncSubstrateInterface",
        lambda *_a, **_kw: fake_substrate,
    )

    p = BittensorProvider()
    await p.connect(ProviderConfig(rpc_url="ws://localhost"))

    assert "additional_headers" not in fake_ws._options


# ---------------------------------------------------------------------------
# Epoch state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("subnet.19.pool.price", 19),
        ("neuron.7.5Fxxx.incentive", 7),
        ("validator.19.5Fxxx.dividends-alpha", 19),
        ("validator.23.5Fxxx.stake-alpha", 23),
        ("validator.5Fxxx.commission", None),
        ("network.subnet-registration-cost", None),
        ("account.5Fxxx.balance", None),
    ],
)
def test_epoch_netuid_for_path(path: str, expected: int | None) -> None:
    assert BittensorProvider().epoch_netuid_for(path) == expected


def test_epoch_netuid_for_validator_weights_uses_explicit_read_arg() -> None:
    provider = BittensorProvider()
    assert provider.epoch_netuid_for("validator.5Fxxx.weights", {"netuid": 19}) == 19
    assert provider.epoch_netuid_for("validator.5Fxxx.weights", {}) == 1


def test_registration_cost_cadence_is_per_block() -> None:
    provider = BittensorProvider()
    assert provider.natural_cadence_for("subnet.3.registration-cost") is Cadence.PER_BLOCK


async def test_get_epoch_state_reads_current_state_at_pinned_block() -> None:
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    substrate = MagicMock()

    async def query(_module: str, storage: str, _params: list[int], **_kwargs: object) -> int:
        return {
            "Tempo": 99,
            "LastEpochBlock": 480,
            "SubnetEpochIndex": 12,
        }[storage]

    substrate.query = AsyncMock(side_effect=query)
    substrate.runtime_call = AsyncMock(return_value=575)
    provider = BittensorProvider()
    provider._substrate = substrate

    state = await provider.get_epoch_state(
        19,
        at_block=BlockRef(number=500, hash="0xabc"),
    )

    assert state.netuid == 19
    assert state.epoch_index == 12
    assert state.last_epoch_block == 480
    assert state.next_epoch_start_block == 575
    assert state.tempo == 99


async def test_blocks_until_immunity_expiry_uses_only_block_distance() -> None:
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    substrate = MagicMock()

    async def query(
        _module: str,
        storage: str,
        _params: list[int],
        **_kwargs: object,
    ) -> int:
        return {"ImmunityPeriod": 7_200, "BlockAtRegistration": 10_000}[storage]

    substrate.query = AsyncMock(side_effect=query)
    provider = BittensorProvider()
    provider._substrate = substrate
    provider._hotkey_uid = AsyncMock(return_value=4)

    remaining = await provider._read_blocks_until_immunity(
        19,
        "5Fxxx",
        12_345,
        "0xabc",
    )

    assert remaining == 4_855
    assert all(call.args[1] != "Tempo" for call in substrate.query.await_args_list)


# ---------------------------------------------------------------------------
# subscribe_storage path mapping
# ---------------------------------------------------------------------------


class _FakeStorageKey:
    def __init__(self, value: str) -> None:
        self._value = value

    def to_hex(self) -> str:
        return self._value


def test_registry_policy_covers_single_key_storage_paths():
    basic_paths = [
        "validator.{hotkey}.commission",
        "account.{coldkey}.balance",
        "subnet.{netuid}.registration-cost",
        "network.subnet-count",
    ]
    for template in basic_paths:
        policy = lookup(template).observation_policy
        assert policy.storage_binding is not None, f"Missing binding: {template!r}"


@pytest.mark.asyncio
async def test_tao_price_uses_native_coin_quote_with_chain_context() -> None:
    source = AsyncMock()
    source.coin_price_usd.return_value = UsdPrice(
        value=190.71,
        last_updated_at=1_722_000_000,
    )
    provider = BittensorProvider(market_prices=MarketPriceFeed(cast("MarketPriceSource", source)))
    observed_at = datetime(2026, 7, 29, 16, 30, tzinfo=UTC)
    provider._resolve_block = AsyncMock(return_value=(5_000_000, "0xabc"))
    provider._block_timestamp = AsyncMock(return_value=observed_at)

    observed = await provider.read_observable("network.tao-price", {})

    assert observed.value == pytest.approx(190.71)
    assert observed.path == "network.tao-price"
    assert observed.block == 5_000_000
    assert observed.block_hash == "0xabc"
    assert observed.timestamp == observed_at
    assert observed.meta == {
        "source": "coingecko",
        "quote_currency": "usd",
        "coin_id": "bittensor",
        "coin_name": "Bittensor",
        "coin_symbol": "TAO",
        "price_last_updated_at": "2024-07-26T13:20:00Z",
    }
    source.coin_price_usd.assert_awaited_once_with(coin_id="bittensor")


def test_storage_map_covers_runtime_version() -> None:
    binding = lookup("network.runtime-version").observation_policy.storage_binding
    assert binding is not None
    assert (binding.module, binding.storage_function, binding.path_params) == (
        "System",
        "LastRuntimeUpgrade",
        (),
    )


async def test_storage_subscription_surfaces_failure_and_meters_setup() -> None:
    class _FailingSubstrate:
        async def create_storage_key(
            self,
            module: str,
            storage_fn: str,
            params: list[object],
        ) -> object:
            assert (module, storage_fn, params) == ("System", "LastRuntimeUpgrade", [])
            return _FakeStorageKey("0xruntime")

        async def rpc_request(
            self,
            method: str,
            params: list[object],
            result_handler: object,
        ) -> None:
            assert method == "state_subscribeStorage"
            assert params == [["0xruntime"]]
            assert result_handler is not None
            raise RuntimeError("subscription exploded")

    charges: list[int] = []
    substrate = cast(AsyncSubstrateInterface, _FailingSubstrate())
    subscription = _StorageSubscription(
        substrate,
        "network.runtime-version",
        charge_rpc=charges.append,
    )

    with pytest.raises(SubscriptionFailedError, match="subscription exploded"):
        await anext(subscription)

    assert charges == [1, 1]
    await subscription.aclose()


async def test_storage_subscription_yields_block_hint_and_meters_metadata() -> None:
    class _OneUpdateSubstrate:
        async def create_storage_key(
            self,
            module: str,
            storage_fn: str,
            params: list[object],
        ) -> object:
            assert (module, storage_fn, params) == ("System", "LastRuntimeUpgrade", [])
            return _FakeStorageKey("0xruntime")

        async def rpc_request(
            self,
            method: str,
            params: list[object],
            result_handler: object,
        ) -> None:
            assert method == "state_subscribeStorage"
            assert params == [["0xruntime"]]
            await cast(Any, result_handler)(
                {"jsonrpc": "2.0", "result": "sub-1"},
                "sub-1",
            )
            await cast(Any, result_handler)(
                {
                    "params": {
                        "result": {
                            "block": "0xnotification",
                            "changes": [["0xruntime", "0x01ba"]],
                        }
                    }
                },
                "sub-1",
            )
            await asyncio.Event().wait()

        async def get_chain_head(self) -> str:
            raise AssertionError("the notification block hash must be used")

        async def get_block_number(self, block_hash: str) -> int:
            assert block_hash == "0xnotification"
            return 123

    charges: list[int] = []
    substrate = cast(AsyncSubstrateInterface, _OneUpdateSubstrate())
    subscription = _StorageSubscription(
        substrate,
        "network.runtime-version",
        charge_rpc=charges.append,
    )

    update = await asyncio.wait_for(anext(subscription), timeout=0.2)

    assert update.path == "network.runtime-version"
    assert update.value == "0x01ba"
    assert update.block == 123
    assert update.block_hash == "0xnotification"
    # Storage-key creation, subscription setup, notification, block number.
    assert charges == [1, 1, 1, 1]
    await subscription.aclose()


def test_storage_map_each_entry_has_valid_substrate_module():
    for entry in all_entries():
        binding = entry.observation_policy.storage_binding
        if binding is None:
            continue
        assert binding.module, f"{entry.path_template}: empty module"
        assert binding.storage_function, f"{entry.path_template}: empty storage function"


def test_only_price_has_composite_pool_storage_policy():
    """Price can wake on either reserve; the other pool values remain head-driven."""
    computed_templates = [
        "subnet.{netuid}.pool.tao-depth",
        "subnet.{netuid}.pool.alpha-depth",
        "subnet.{netuid}.pool.depth-for-trade",
    ]
    for template in computed_templates:
        assert lookup(template).observation_policy.storage_bindings == ()

    price_bindings = lookup("subnet.{netuid}.pool.price").observation_policy.storage_bindings
    assert [binding.storage_function for binding in price_bindings] == [
        "SubnetTAO",
        "SubnetAlphaIn",
    ]


def test_burn_rate_uses_raw_storage_with_epoch_fallback():
    burn_policy = lookup("subnet.{netuid}.burn-rate").observation_policy
    registration_policy = lookup("subnet.{netuid}.registration-cost").observation_policy

    assert burn_policy.driver_for("threshold") == ObservationDriver.STORAGE_CHANGE
    assert burn_policy.fallback_driver == ObservationDriver.SUBNET_EPOCH
    assert burn_policy.storage_bindings[0].storage_function == "MinerBurned"
    assert registration_policy.storage_binding is not None
    assert registration_policy.storage_binding.storage_function == "Burn"


async def test_composite_storage_subscription_creates_and_subscribes_every_key() -> None:
    class _CompositeSubstrate:
        def __init__(self) -> None:
            self.created: list[tuple[str, str, list[object]]] = []

        async def create_storage_key(
            self,
            module: str,
            storage_fn: str,
            params: list[object],
        ) -> object:
            self.created.append((module, storage_fn, params))
            return _FakeStorageKey(storage_fn)

        async def rpc_request(
            self,
            method: str,
            params: list[object],
            result_handler: object,
        ) -> None:
            assert method == "state_subscribeStorage"
            assert params == [["SubnetTAO", "SubnetAlphaIn"]]
            await cast(Any, result_handler)(
                {
                    "params": {
                        "result": {
                            "block": "0xnotification",
                            "changes": [
                                ["SubnetTAO", "0x01"],
                                ["SubnetAlphaIn", "0x02"],
                            ],
                        }
                    }
                },
                "sub-1",
            )
            await asyncio.Event().wait()

        async def get_chain_head(self) -> str:
            raise AssertionError("the notification block hash must be used")

        async def get_block_number(self, block_hash: str) -> int:
            assert block_hash == "0xnotification"
            return 123

    charges: list[int] = []
    substrate = _CompositeSubstrate()
    subscription = _StorageSubscription(
        cast(AsyncSubstrateInterface, substrate),
        "subnet.28.pool.price",
        charge_rpc=charges.append,
    )

    update = await asyncio.wait_for(anext(subscription), timeout=0.2)

    assert substrate.created == [
        ("SubtensorModule", "SubnetTAO", ["28"]),
        ("SubtensorModule", "SubnetAlphaIn", ["28"]),
    ]
    assert update.path == "subnet.28.pool.price"
    assert update.block == 123
    assert update.block_hash == "0xnotification"
    # Two key creations, subscription setup, one notification, block number.
    assert charges == [1, 1, 1, 1, 1]
    await subscription.aclose()


# ---------------------------------------------------------------------------
# Current subnet emission and miner-burn semantics (spec 440)
# ---------------------------------------------------------------------------


class _EmissionSubstrate:
    """Stand-in for the spec-440 emission storage and runtime API."""

    def __init__(
        self,
        *,
        tao_in: int = 300_000_000,
        excess_tao: int = 200_000_000,
        block_emission: int = 1_000_000_000,
        miner_burned_bits: int = 0,
    ) -> None:
        self.values = {
            "SubnetTaoInEmission": tao_in,
            "SubnetExcessTao": excess_tao,
            "MinerBurned": {"bits": miner_burned_bits},
        }
        self.block_emission = block_emission
        self.calls: list[tuple[str, str, object, str]] = []

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        self.calls.append(("query", f"{module}.{storage_fn}", params, block_hash))
        return _ScaleType(self.values.get(storage_fn, 0))

    async def runtime_call(
        self,
        api: str,
        method: str,
        params: object,
        block_hash: str = "",
    ) -> int:
        self.calls.append(("runtime_call", f"{api}.{method}", params, block_hash))
        return self.block_emission


@pytest.mark.asyncio
async def test_emission_share_includes_pool_injection_and_excess_tao() -> None:
    """All TAO routed to a subnet is its share of the block's total emission."""
    substrate = _EmissionSubstrate()
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    result = await provider._read_subnet_emission_share(19, "0xpinned")

    assert result == pytest.approx(0.5)
    assert substrate.calls == [
        (
            "query",
            "SubtensorModule.SubnetTaoInEmission",
            [19],
            "0xpinned",
        ),
        (
            "query",
            "SubtensorModule.SubnetExcessTao",
            [19],
            "0xpinned",
        ),
        (
            "runtime_call",
            "SubnetInfoRuntimeApi.get_block_emission",
            [],
            "0xpinned",
        ),
    ]


@pytest.mark.asyncio
async def test_emission_share_handles_zero_block_emission() -> None:
    substrate = _EmissionSubstrate(block_emission=0)
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    assert await provider._read_subnet_emission_share(19, "0xpinned") == 0.0


def test_emission_share_cadence_is_per_block() -> None:
    """The routed TAO components and block-emission denominator update per block."""
    provider = BittensorProvider()
    assert provider.natural_cadence_for("subnet.19.emission-share") == Cadence.PER_BLOCK


@pytest.mark.asyncio
async def test_burn_rate_reads_last_tempo_miner_burned_fraction() -> None:
    """MinerBurned is a U96F32 fraction updated when the subnet epoch settles."""
    substrate = _EmissionSubstrate(miner_burned_bits=1 << 31)
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    result = await provider._read_subnet_burn_rate(19, "0xpinned")

    assert result == pytest.approx(0.5)
    assert provider.natural_cadence_for("subnet.19.burn-rate") == Cadence.PER_EPOCH
    assert substrate.calls == [
        ("query", "SubtensorModule.MinerBurned", [19], "0xpinned"),
    ]


# ---------------------------------------------------------------------------
# get_block_finality — search horizon and not-found semantics
# ---------------------------------------------------------------------------


def _raw_extrinsic_hash(encoded: str) -> str:
    """Return the canonical Substrate hash for an encoded extrinsic."""

    return "0x" + blake2b(bytes.fromhex(encoded[2:]), digest_size=32).hexdigest()


@pytest.mark.asyncio
async def test_tx_scan_hashes_raw_extrinsics_without_historical_runtime_state() -> None:
    """Transaction lookup must not decode blocks through pruned runtime state."""
    raw_extrinsic = "0x01020304"
    expected_hash = _raw_extrinsic_hash(raw_extrinsic)

    class _NonArchiveSubstrate:
        async def get_block_hash(self, block_num: int) -> str:
            assert block_num == 7
            return "0xblock7"

        async def get_extrinsics(self, block_hash: str) -> object:
            raise AssertionError(f"decoded get_extrinsics must not be called for {block_hash}")

        async def rpc_request(
            self,
            method: str,
            params: list[object],
        ) -> dict[str, object]:
            assert method == "chain_getBlock"
            assert params == ["0xblock7"]
            return {
                "result": {
                    "block": {
                        "header": {},
                        "extrinsics": [raw_extrinsic],
                    }
                }
            }

    result = await BittensorProvider._block_extrinsic_hashes(
        cast(AsyncSubstrateInterface, _NonArchiveSubstrate()),
        7,
    )

    assert result == [("0xblock7", expected_hash)]


@pytest.mark.asyncio
async def test_tx_scan_reports_pruned_block_body_as_archive_requirement() -> None:
    """A missing raw block body is terminal and must name the required remedy."""

    class _PrunedBodySubstrate:
        async def get_block_hash(self, block_num: int) -> str:
            assert block_num == 7
            return "0xblock7"

        async def rpc_request(
            self,
            method: str,
            params: list[object],
        ) -> dict[str, object]:
            assert method == "chain_getBlock"
            assert params == ["0xblock7"]
            return {"result": None}

    with pytest.raises(TxNotFoundInHorizonError, match="archive node"):
        await BittensorProvider._block_extrinsic_hashes(
            cast(AsyncSubstrateInterface, _PrunedBodySubstrate()),
            7,
        )


class _FinalitySubstrate:
    """AsyncSubstrateInterface stand-in for get_block_finality tests.

    Models a chain with ``head_num``, an optional ``finalized_num``, and a
    mapping from block_num → list of extrinsic hashes. ``timestamp_ms`` (when
    set) makes ``Timestamp.Now`` succeed; ``None`` simulates a degraded chain.
    """

    def __init__(
        self,
        head_num: int,
        *,
        finalized_num: int | None = None,
        blocks: dict[int, list[str]] | None = None,
        timestamp_ms: int | None = 1_700_000_000_000,
        block_hashes: dict[int, str] | None = None,
    ) -> None:
        self._head_num = head_num
        self._finalized_num = finalized_num if finalized_num is not None else head_num
        self._blocks = blocks or {}
        self._timestamp_ms = timestamp_ms
        self._block_hashes = block_hashes or {}
        self.queried_blocks: list[int] = []
        self.rpc_calls: list[str] = []

    def _canonical_hash(self, block_num: int) -> str:
        return self._block_hashes.get(block_num, f"0xblock{block_num}")

    def _block_number_for_hash(self, block_hash: str) -> int | None:
        for block_num, canonical_hash in self._block_hashes.items():
            if canonical_hash == block_hash:
                return block_num
        if block_hash.startswith("0xblock"):
            suffix = block_hash.removeprefix("0xblock").split("-", maxsplit=1)[0]
            return int(suffix)
        return None

    async def get_chain_head(self) -> str:
        self.rpc_calls.append("get_chain_head")
        return self._canonical_hash(self._head_num)

    async def get_chain_finalised_head(self) -> str:
        self.rpc_calls.append("get_chain_finalised_head")
        return self._canonical_hash(self._finalized_num)

    async def get_block_number(self, block_hash: str) -> int | None:
        self.rpc_calls.append("get_block_number")
        return self._block_number_for_hash(block_hash)

    async def get_block_hash(self, block_num: int) -> str | None:
        self.rpc_calls.append("get_block_hash")
        self.queried_blocks.append(block_num)
        return self._canonical_hash(block_num)

    async def rpc_request(
        self,
        method: str,
        params: list[object],
    ) -> dict[str, object]:
        assert method == "chain_getBlock"
        self.rpc_calls.append("chain_getBlock")
        block_hash = str(params[0])
        block_num = self._block_number_for_hash(block_hash)
        assert block_num is not None
        return {
            "result": {
                "block": {
                    "header": {},
                    "extrinsics": self._blocks.get(block_num, []),
                }
            }
        }

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType | None:
        self.rpc_calls.append("query")
        if module == "Timestamp" and storage_fn == "Now":
            if self._timestamp_ms is None:
                return None
            return _ScaleType(self._timestamp_ms)
        return _ScaleType(0)


@pytest.mark.asyncio
async def test_tx_search_horizon_constant_is_7200() -> None:
    """Default horizon is 7200 blocks (~24h on FAST_BLOCKS / mainnet)."""
    assert _TX_SEARCH_HORIZON_BLOCKS == 7200


@pytest.mark.asyncio
async def test_get_block_finality_finds_included_tx() -> None:
    """A tx in a recent block (between finalized and head) returns 'included'."""
    raw_extrinsic = "0x01"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(
        head_num=10_000,
        finalized_num=9_990,
        blocks={9_995: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.tx_hash == tx
    assert status.level == "included"
    assert status.block == 9_995
    assert status.block_hash == "0xblock9995"
    assert status.timestamp is not None


@pytest.mark.asyncio
async def test_get_block_finality_finds_finalized_tx() -> None:
    """A tx at or before the finalised head returns 'finalized'."""
    raw_extrinsic = "0x02"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(
        head_num=10_000,
        finalized_num=9_990,
        blocks={9_980: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.level == "finalized"
    assert status.block == 9_980


@pytest.mark.asyncio
async def test_fresh_scan_does_not_finalize_reorged_out_block() -> None:
    """A scan match must still be canonical when finality is evaluated."""
    tx = "0x" + "ce" * 32
    sub = _FinalitySubstrate(
        head_num=100,
        finalized_num=99,
        block_hashes={99: "0xcanonical"},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)
    p._scan_for_tx = AsyncMock(return_value=(99, "0xstale"))

    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert tx not in p._tx_scan_state


@pytest.mark.asyncio
async def test_get_block_finality_pending_when_chain_too_young() -> None:
    """On a fresh chain (head < horizon), unfound tx returns 'pending'."""
    tx = "0x" + "ef" * 32
    sub = _FinalitySubstrate(head_num=100, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert status.block is None


@pytest.mark.asyncio
async def test_get_block_finality_mature_missing_tx_remains_pending() -> None:
    """A bounded historical miss must not terminate a wake for future inclusion."""
    tx = "0x" + "11" * 32
    sub = _FinalitySubstrate(head_num=_TX_SEARCH_HORIZON_BLOCKS + 100, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert len(sub.queried_blocks) == _TX_SEARCH_HORIZON_BLOCKS


@pytest.mark.asyncio
async def test_get_block_finality_pending_at_exact_horizon_boundary() -> None:
    """Boundary: a complete historical miss still waits for a future block."""
    tx = "0x" + "22" * 32
    sub = _FinalitySubstrate(head_num=_TX_SEARCH_HORIZON_BLOCKS, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.level == "pending"


@pytest.mark.asyncio
async def test_get_block_finality_pending_just_below_horizon() -> None:
    """head_num == horizon - 1: still 'too young', returns pending."""
    tx = "0x" + "33" * 32
    sub = _FinalitySubstrate(head_num=_TX_SEARCH_HORIZON_BLOCKS - 1, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.level == "pending"


@pytest.mark.asyncio
async def test_get_block_finality_searches_horizon_blocks_max() -> None:
    """Search walks back exactly _TX_SEARCH_HORIZON_BLOCKS blocks."""
    tx = "0x" + "44" * 32
    head = _TX_SEARCH_HORIZON_BLOCKS + 500
    sub = _FinalitySubstrate(head_num=head, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    # Walks from head down to head - horizon + 1 (inclusive) = horizon blocks total.
    assert status.level == "pending"
    assert len(sub.queried_blocks) == _TX_SEARCH_HORIZON_BLOCKS
    assert sub.queried_blocks[0] == head
    assert sub.queried_blocks[-1] == head - _TX_SEARCH_HORIZON_BLOCKS + 1


@pytest.mark.asyncio
async def test_get_block_finality_same_head_does_not_rescan_history() -> None:
    """After bootstrap, polling an unchanged head is O(1), not 7,200 block reads."""
    tx = "0x" + "45" * 32
    head = _TX_SEARCH_HORIZON_BLOCKS + 500
    sub = _FinalitySubstrate(head_num=head, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    assert (await p.get_block_finality(tx)).level == "pending"
    bootstrap_reads = len(sub.queried_blocks)
    assert (await p.get_block_finality(tx)).level == "pending"

    assert bootstrap_reads == _TX_SEARCH_HORIZON_BLOCKS
    assert len(sub.queried_blocks) == bootstrap_reads


@pytest.mark.asyncio
async def test_get_block_finality_scans_only_blocks_after_previous_head() -> None:
    """A pending watch scans each newly produced block once after bootstrap."""
    raw_extrinsic = "0x03"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    head = _TX_SEARCH_HORIZON_BLOCKS + 500
    sub = _FinalitySubstrate(head_num=head, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    assert (await p.get_block_finality(tx)).level == "pending"
    sub.queried_blocks.clear()
    sub._head_num = head + 3
    sub._finalized_num = head + 1
    sub._blocks[head + 2] = [raw_extrinsic]

    status = await p.get_block_finality(tx)

    assert status.level == "included"
    assert status.block == head + 2
    assert sub.queried_blocks == [head, head + 3, head + 2, head + 2]


@pytest.mark.asyncio
async def test_get_block_finality_caches_inclusion_while_waiting_for_finality() -> None:
    """Once included, finality polling reuses block metadata without rescanning."""
    raw_extrinsic = "0x04"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(
        head_num=100,
        finalized_num=95,
        blocks={99: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    included = await p.get_block_finality(tx)
    block_reads = len(sub.queried_blocks)
    sub._finalized_num = 99
    finalized = await p.get_block_finality(tx)

    assert included.level == "included"
    assert finalized.level == "finalized"
    assert finalized.block == included.block
    assert finalized.block_hash == included.block_hash
    assert finalized.timestamp == included.timestamp
    assert sub.queried_blocks[block_reads:] == [included.block]


@pytest.mark.asyncio
async def test_get_block_finality_restarts_bounded_scan_after_inclusion_reorg() -> None:
    """A cached but unfinalized inclusion must not survive a canonical reorg."""
    raw_extrinsic = "0x05"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(
        head_num=100,
        finalized_num=95,
        blocks={99: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    assert (await p.get_block_finality(tx)).level == "included"
    sub._blocks.pop(99)
    sub._block_hashes[99] = "0xblock98"

    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert p._tx_scan_state[tx].included is None


@pytest.mark.asyncio
async def test_cached_inclusion_reorg_is_checked_before_finality_crossing() -> None:
    """A reorged inclusion must not become finalized merely because finality advanced."""
    raw_extrinsic = "0x06"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(
        head_num=100,
        finalized_num=95,
        blocks={99: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    included = await p.get_block_finality(tx)
    assert included.level == "included"

    sub._blocks.pop(99)
    sub._block_hashes[99] = "0xblock99-reorg"
    sub._finalized_num = 99

    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert status.block is None
    assert p._tx_scan_state[tx].included is None


@pytest.mark.asyncio
async def test_cached_inclusion_checks_canonical_hash_after_finality_snapshot() -> None:
    """A reorg during finality lookup must not promote the stale cached hash."""
    raw_extrinsic = "0x07"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(
        head_num=100,
        finalized_num=95,
        blocks={99: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    included = await p.get_block_finality(tx)
    assert included.level == "included"

    async def reorg_then_return_finalized_head() -> str:
        sub._blocks.pop(99)
        sub._block_hashes[99] = "0xblock99-reorg"
        sub._finalized_num = 99
        return "0xblock99-reorg"

    sub.get_chain_finalised_head = reorg_then_return_finalized_head  # ty: ignore[invalid-assignment]

    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert status.block is None


@pytest.mark.asyncio
async def test_advancing_head_rescans_when_prior_cursor_block_was_reorged() -> None:
    """A tx inserted below the old cursor by a reorg must still be discovered."""
    raw_extrinsic = "0x08"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    sub = _FinalitySubstrate(head_num=100, finalized_num=95, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    assert (await p.get_block_finality(tx)).level == "pending"

    sub._head_num = 102
    sub._finalized_num = 98
    sub._block_hashes[100] = "0xblock100-reorg"
    sub._blocks[99] = [raw_extrinsic]

    status = await p.get_block_finality(tx)

    assert status.level == "included"
    assert status.block == 99
    assert status.block_hash == "0xblock99"


@pytest.mark.asyncio
async def test_tx_read_observable_has_no_redundant_outer_head_pinning() -> None:
    """A routine pending tx tick performs exactly the work declared in read_cost."""
    tx = "0x" + "4c" * 32
    sub = _FinalitySubstrate(head_num=100, finalized_num=95, blocks={})
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    first = await p.read_observable(f"tx.{tx}", {})
    assert cast(TxFinalityStatus, first.value).level == "pending"

    sub._head_num = 101
    sub.rpc_calls.clear()
    observation = await p.read_observable(f"tx.{tx}", {})

    assert cast(TxFinalityStatus, observation.value).level == "pending"
    assert observation.block == 101
    assert observation.block_hash == "0xblock101"
    assert sub.rpc_calls == [
        "get_chain_head",
        "get_block_number",
        "get_block_hash",
        "get_block_hash",
        "chain_getBlock",
        "query",
    ]
    assert lookup("tx.{tx_hash}").read_cost == len(sub.rpc_calls)


@pytest.mark.asyncio
async def test_get_block_finality_propagates_rate_limit_during_scan() -> None:
    """A rate-limit response must leave the retry layer in control."""
    tx = "0x" + "48" * 32
    sub = _FinalitySubstrate(head_num=100, blocks={})

    async def rate_limited(_block_num: int) -> str | None:
        raise SubstrateRequestException({"code": -32029, "message": "rate limit exceeded"})

    cast(Any, sub).get_block_hash = rate_limited
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    with pytest.raises(RateLimitError):
        await p.get_block_finality(tx)


@pytest.mark.asyncio
async def test_get_block_finality_retry_resumes_mid_bootstrap_scan() -> None:
    """Completed bootstrap heights are not reread after a transient failure."""
    tx = "0x" + "50" * 32
    head = 10
    failed_block = 8
    sub = _FinalitySubstrate(head_num=head, blocks={})
    failed_once = False

    async def fail_once(block_num: int) -> str | None:
        nonlocal failed_once
        sub.queried_blocks.append(block_num)
        if block_num == failed_block and not failed_once:
            failed_once = True
            raise SubstrateRequestException({"code": -32029, "message": "rate limit exceeded"})
        return f"0xblock{block_num}"

    cast(Any, sub).get_block_hash = fail_once
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    with pytest.raises(RateLimitError):
        await p.get_block_finality(tx)
    status = await p.get_block_finality(tx)

    assert status.level == "pending"
    assert sub.queried_blocks == [10, 9, 8, 8, 7, 6, 5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_get_block_finality_old_tx_within_horizon_is_found() -> None:
    """A tx >200 blocks old but within 7200-block horizon is now found."""
    raw_extrinsic = "0x09"
    tx = _raw_extrinsic_hash(raw_extrinsic)
    head = 10_000
    sub = _FinalitySubstrate(
        head_num=head,
        finalized_num=head - 5,
        # Far older than the previous 200-block depth.
        blocks={head - 5_000: [raw_extrinsic]},
    )
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    status = await p.get_block_finality(tx)

    assert status.level == "finalized"
    assert status.block == head - 5_000


@pytest.mark.asyncio
async def test_tx_not_found_in_horizon_is_provider_error_subclass() -> None:
    """TxNotFoundInHorizonError is a ProviderError so the runtime maps it."""
    from chainwake.core.errors import ProviderError  # noqa: PLC0415

    err = TxNotFoundInHorizonError("test")
    assert isinstance(err, ProviderError)
    assert err.reason == "subscription_failed"


@pytest.mark.asyncio
async def test_tx_not_found_in_horizon_is_not_transient() -> None:
    """The new error is NOT in the transient retry set (would loop forever)."""
    from chainwake.core.retry import _is_transient  # noqa: PLC0415

    err = TxNotFoundInHorizonError("test")
    assert _is_transient(err) is False


# ---------------------------------------------------------------------------
# _block_timestamp — no silent wallclock fallback
# ---------------------------------------------------------------------------


class _TimestampOnlySubstrate:
    """Stand-in for AsyncSubstrateInterface that controls Timestamp.Now only."""

    def __init__(self, *, return_value: object | None, raise_exc: Exception | None = None) -> None:
        self._return_value = return_value
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> object | None:
        self.calls.append((module, storage_fn))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._return_value


class _BoomError(Exception):
    """Test sentinel exception for substrate-level failures."""


@pytest.mark.asyncio
async def test_block_timestamp_returns_chain_timestamp() -> None:
    """Successful Timestamp.Now read decodes to a UTC datetime."""
    sub = _TimestampOnlySubstrate(return_value=_ScaleType(1_700_000_000_000))
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    ts = await p._block_timestamp("0xabc")

    assert ts.tzinfo is not None
    assert ts.timestamp() == 1_700_000_000.0
    assert sub.calls == [("Timestamp", "Now")]


@pytest.mark.asyncio
async def test_block_timestamp_propagates_substrate_exception() -> None:
    """A substrate query failure propagates — no silent wallclock fallback."""
    sub = _TimestampOnlySubstrate(return_value=None, raise_exc=_BoomError("rpc dropped"))
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    with pytest.raises(_BoomError, match="rpc dropped"):
        await p._block_timestamp("0xabc")


@pytest.mark.asyncio
async def test_block_timestamp_raises_decode_error_on_none() -> None:
    """A None response from Timestamp.Now raises DecodeError, not wallclock."""
    sub = _TimestampOnlySubstrate(return_value=None)
    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, sub)

    with pytest.raises(DecodeError, match=r"Timestamp\.Now"):
        await p._block_timestamp("0xabc")


@pytest.mark.asyncio
async def test_block_timestamp_failure_propagates_through_read_observable() -> None:
    """read_observable surfaces _block_timestamp failures so the runtime maps them."""

    class _FlakyTimestamp:
        async def get_chain_head(self) -> str:
            return "0xhead1"

        async def get_block_number(self, block_hash: str) -> int:
            return 1

        async def query(
            self,
            module: str,
            storage_fn: str,
            params: list[object] | None = None,
            block_hash: str = "",
        ) -> _ScaleType:
            if module == "Timestamp":
                raise _BoomError("timestamp rpc broke")
            return _ScaleType(0)

    p = BittensorProvider()
    p._substrate = cast(AsyncSubstrateInterface, _FlakyTimestamp())
    with pytest.raises(_BoomError):
        await p.read_observable("network.subnet-count", {})


@pytest.mark.asyncio
async def test_weight_liveness_read_includes_historical_activity_context() -> None:
    """LastUpdate metadata must point to the marker block, not watcher arrival."""

    class _HistoricalContextSubstrate:
        async def get_block_hash(self, block_number: int) -> str:
            assert block_number == 800
            return "0xactivity"

        async def query(
            self,
            module: str,
            storage_fn: str,
            params: list[object] | None = None,
            block_hash: str = "",
        ) -> _ScaleType:
            assert (module, storage_fn, params, block_hash) == (
                "SubtensorModule",
                "SubnetEpochIndex",
                [19],
                "0xactivity",
            )
            return _ScaleType(16)

    current_ts = datetime(2026, 5, 5, 12, tzinfo=UTC)
    activity_ts = datetime(2026, 5, 5, 10, tzinfo=UTC)
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _HistoricalContextSubstrate())
    provider._resolve_block = AsyncMock(return_value=(1_000, "0xcurrent"))
    provider._block_timestamp = AsyncMock(side_effect=[current_ts, activity_ts])
    provider._dispatch = AsyncMock(return_value=800)
    hotkey = ALICE_SS58

    observation = await provider.read_observable(
        f"validator.{hotkey}.weights",
        {"netuid": 19, "mechid": 0},
    )

    assert observation.meta["activity_timestamp"] == activity_ts
    assert observation.meta["activity_epoch_index"] == 16


# ---------------------------------------------------------------------------
# _read_validator_identity — IdentitiesV2 storage decode
# ---------------------------------------------------------------------------


class _IdentitySubstrate:
    """AsyncSubstrateInterface stand-in returning a fixed IdentitiesV2 entry.

    Storage in Substrate's pallet-identity returns each ``BoundedVec<u8>``
    field as a hex-prefixed string; ``decode_hex_identity_dict`` is what
    converts those to UTF-8 strings.  Tests pass the raw hex shape so the
    provider's decode path is exercised end-to-end.
    """

    def __init__(self, identity_value: dict[str, object] | None) -> None:
        self._value = identity_value
        self.calls: list[tuple[str, str, list[object], str]] = []

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        self.calls.append((module, storage_fn, list(params or []), block_hash))
        return _ScaleType(self._value)


@pytest.mark.asyncio
async def test_read_validator_identity_decodes_hex_fields() -> None:
    """Hex-encoded BoundedVec fields decode to UTF-8 strings via the helper."""
    provider = BittensorProvider()
    raw: dict[str, object] = {
        "name": "0x416c696365",  # "Alice"
        "url": "0x6578616d706c652e636f6d",  # "example.com"
        "github_repo": "0x676974687562",  # "github"
        "image": "",
        "discord": "0x646973636f7264",  # "discord"
        "description": "0x68656c6c6f",  # "hello"
        "additional": "",
    }
    fake = _IdentitySubstrate(identity_value=raw)
    provider._substrate = cast(AsyncSubstrateInterface, fake)

    result = await provider._read_validator_identity("5Fxxx", "0xabc")

    assert result == {
        "name": "Alice",
        "url": "example.com",
        "github": "github",
        "image": "",
        "discord": "discord",
        "description": "hello",
        "additional": "",
    }
    assert fake.calls == [("SubtensorModule", "IdentitiesV2", ["5Fxxx"], "0xabc")]


@pytest.mark.asyncio
async def test_read_validator_identity_unset_returns_empty_dict() -> None:
    """No identity set for hotkey → stable empty dict (not None) for state primitive."""
    provider = BittensorProvider()
    fake = _IdentitySubstrate(identity_value=None)
    provider._substrate = cast(AsyncSubstrateInterface, fake)

    result = await provider._read_validator_identity("5Fxxx", "0xabc")

    assert result == {}
    assert fake.calls == [("SubtensorModule", "IdentitiesV2", ["5Fxxx"], "0xabc")]


@pytest.mark.asyncio
async def test_read_validator_identity_stable_across_ticks_when_unset() -> None:
    """Two consecutive reads of an unset identity must compare equal.

    The state primitive's ``--on-change`` semantics rely on equality between
    consecutive ObservableValue.value reads.  An unset identity must never
    look like a state transition.
    """
    provider = BittensorProvider()
    fake = _IdentitySubstrate(identity_value=None)
    provider._substrate = cast(AsyncSubstrateInterface, fake)

    first = await provider._read_validator_identity("5Fxxx", "0xabc")
    second = await provider._read_validator_identity("5Fxxx", "0xdef")

    assert first == second


@pytest.mark.asyncio
async def test_read_validator_identity_routed_via_dispatch() -> None:
    """The validator dispatcher routes the identity template to the new read fn."""
    provider = BittensorProvider()
    raw: dict[str, object] = {
        "name": "0x426f62",  # "Bob"
        "url": "",
        "github_repo": "",
        "image": "",
        "discord": "",
        "description": "",
        "additional": "",
    }
    fake = _IdentitySubstrate(identity_value=raw)
    provider._substrate = cast(AsyncSubstrateInterface, fake)

    value = await provider._dispatch_validator(
        "validator.{hotkey}.identity", ["validator", "5Fxxx"], {}, "0xabc"
    )

    assert isinstance(value, dict)
    decoded = cast("dict[str, object]", value)
    assert decoded["name"] == "Bob"


def test_validator_identity_listed_in_storage_subscription_map() -> None:
    """``subscribe_storage`` for the new path must resolve to IdentitiesV2."""
    binding = lookup("validator.{hotkey}.identity").observation_policy.storage_binding
    assert binding is not None
    assert (binding.module, binding.storage_function, binding.path_params) == (
        "SubtensorModule",
        "IdentitiesV2",
        ("hotkey",),
    )


# ---------------------------------------------------------------------------
# Pool alpha-supply, moving-price, volume reads
# ---------------------------------------------------------------------------


_FAKE_DYNAMIC_INFO: dict[str, object] = {
    "netuid": 19,
    "tao_in": 2_000_000_000,  # 2 TAO
    "alpha_in": 10_000_000_000,  # 10 alpha
    "alpha_out": 5_000_000_000,  # 5 alpha (supply outside pool)
    "subnet_volume": 3_000_000_000,  # 3 TAO cumulative volume
    # moving_price is U32F32: bits / (1 << 32) → float price.
    # Verified against finney where bits=58611784 → 0.01365, matching
    # spot price 0.01371 within EMA lag.
    "moving_price": {"bits": int(0.013 * (1 << 32))},  # ≈ 0.013 TAO/alpha
}


class _DynamicInfoSubstrate:
    """Stand-in that returns _FAKE_DYNAMIC_INFO from runtime_call."""

    def __init__(self, info: dict[str, object]) -> None:
        self._info = info

    async def runtime_call(
        self, api: str, method: str, params: object, block_hash: str = ""
    ) -> dict[str, object]:
        return self._info

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        if storage_fn == "NetworksAdded":
            return _ScaleType(True)
        return _ScaleType(0)


class _NetworksAddedSubstrate:
    """Stand-in returning a fixed ``NetworksAdded`` value for every query."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> object:
        assert storage_fn == "NetworksAdded"
        return self._value


@pytest.mark.asyncio
async def test_require_subnet_false_raises_user_error() -> None:
    """A genuinely unregistered netuid decodes to False, not None."""
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _NetworksAddedSubstrate(_ScaleType(False)))

    with pytest.raises(UserError, match="subnet 19 does not exist"):
        await provider._require_subnet(19, "0xabc")


@pytest.mark.asyncio
async def test_require_subnet_none_raises_rpc_unreachable_not_user_error() -> None:
    """A dropped/incomplete response must not be mistaken for a missing subnet.

    Regression: under rate limiting, a batched ``NetworksAdded`` query can
    come back with no result at all rather than raising. Treating that as
    "subnet does not exist" skips retry/backoff and reports a real subnet
    as absent.
    """
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _NetworksAddedSubstrate(None))

    with pytest.raises(RPCUnreachableError, match="returned no result"):
        await provider._require_subnet(19, "0xabc")


@pytest.mark.asyncio
async def test_read_subnet_alpha_supply_returns_tao() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(_FAKE_DYNAMIC_INFO))
    result = await provider._read_subnet_alpha_supply(19, "0xabc")
    assert result == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_read_subnet_moving_price_u32f32_conversion() -> None:
    """``moving_price`` is U32F32: ``bits / (1 << 32)`` must give the float price."""
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(_FAKE_DYNAMIC_INFO))
    result = await provider._read_subnet_moving_price(19, "0xabc")
    assert result == pytest.approx(0.013, rel=1e-6)


@pytest.mark.asyncio
async def test_read_subnet_moving_price_missing_returns_zero() -> None:
    info: dict[str, object] = {**_FAKE_DYNAMIC_INFO, "moving_price": None}
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(info))
    result = await provider._read_subnet_moving_price(19, "0xabc")
    assert result == 0.0


@pytest.mark.asyncio
async def test_read_subnet_moving_price_non_dict_returns_zero() -> None:
    info: dict[str, object] = {**_FAKE_DYNAMIC_INFO, "moving_price": 42}
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(info))
    result = await provider._read_subnet_moving_price(19, "0xabc")
    assert result == 0.0


@pytest.mark.asyncio
async def test_read_subnet_volume_returns_tao() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(_FAKE_DYNAMIC_INFO))
    result = await provider._read_subnet_volume(19, "0xabc")
    assert result == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_dispatch_subnet_routes_alpha_supply() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(_FAKE_DYNAMIC_INFO))
    value = await provider._dispatch_subnet(
        "subnet.{netuid}.pool.alpha-supply", ["subnet", "19"], {}, 100, "0xabc"
    )
    assert isinstance(value, float)
    assert value == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_dispatch_subnet_routes_moving_price() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(_FAKE_DYNAMIC_INFO))
    value = await provider._dispatch_subnet(
        "subnet.{netuid}.pool.moving-price", ["subnet", "19"], {}, 100, "0xabc"
    )
    assert isinstance(value, float)
    assert value == pytest.approx(0.013, rel=1e-6)


@pytest.mark.asyncio
async def test_dispatch_subnet_routes_volume() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _DynamicInfoSubstrate(_FAKE_DYNAMIC_INFO))
    value = await provider._dispatch_subnet(
        "subnet.{netuid}.pool.volume", ["subnet", "19"], {}, 100, "0xabc"
    )
    assert isinstance(value, float)


# ---------------------------------------------------------------------------
# _read_subnet_hyperparams — batched multi-param read
# ---------------------------------------------------------------------------

_PARAM_KEYS = [
    "tempo",
    "immunity_period",
    "min_allowed_weights",
    "max_weights_limit",
    "max_allowed_validators",
    "max_allowed_uids",
    "activity_cutoff_factor_milli",
    "activity_cutoff",
    "adjustment_interval",
    "weights_version_key",
    "weights_set_rate_limit",
    "kappa",
    "rho",
]
_PARAM_VALUES = [99, 7200, 1, 65535, 64, 4096, 50_000, 112, 0, 100, 32767, 10]


class _HyperparamsSubstrate:
    """Stub that supports query_multi for hyperparams batched reads."""

    async def create_storage_key(
        self, module: str, storage_fn: str, params: list[object]
    ) -> tuple[str, str, list[object]]:
        return (module, storage_fn, params)

    async def query_multi(
        self,
        storage_keys: list[object],
        block_hash: str = "",
    ) -> list[tuple[object, _ScaleType]]:
        return [
            (key, _ScaleType(val)) for key, val in zip(storage_keys, _PARAM_VALUES, strict=False)
        ]

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        if storage_fn == "NetworksAdded":
            return _ScaleType(True)
        return _ScaleType(0)


@pytest.mark.asyncio
async def test_read_subnet_hyperparams_returns_all_keys() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _HyperparamsSubstrate())
    result = await provider._read_subnet_hyperparams(19, "0xabc")
    assert isinstance(result, dict)
    assert set(result.keys()) == set(_PARAM_KEYS)


@pytest.mark.asyncio
async def test_read_subnet_hyperparams_values_correct() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _HyperparamsSubstrate())
    result = await provider._read_subnet_hyperparams(19, "0xabc")
    assert result["tempo"] == 99
    assert result["immunity_period"] == 7200
    assert result["kappa"] == 32767


@pytest.mark.parametrize(
    "error",
    [
        RPCUnreachableError("temporary websocket failure"),
        RateLimitError("rate limit exceeded"),
    ],
)
async def test_read_subnet_hyperparams_propagates_provider_errors(error: Exception) -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _HyperparamsSubstrate())
    provider._query_multi_int = AsyncMock(side_effect=error)

    with pytest.raises(type(error), match=str(error)):
        await provider._read_subnet_hyperparams(19, "0xabc")


@pytest.mark.parametrize(
    "error",
    [
        AuthError("invalid API key"),
        RPCUnreachableError("temporary websocket failure"),
        RateLimitError("rate limit exceeded"),
    ],
)
async def test_read_validator_child_keys_propagates_provider_errors(
    error: Exception,
) -> None:
    substrate = AsyncMock()
    substrate.query.return_value = _ScaleType(2)
    substrate.create_storage_key.side_effect = error
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    with pytest.raises(type(error), match=str(error)):
        await provider._read_validator_child_keys("5Validator", "0xabc")


@pytest.mark.asyncio
async def test_read_validator_child_keys_treats_missing_values_as_empty() -> None:
    substrate = AsyncMock()
    substrate.query.return_value = _ScaleType(2)
    substrate.create_storage_key.side_effect = ["key-1", "key-2"]
    substrate.query_multi.return_value = [
        ("key-1", _ScaleType(None)),
        ("key-2", _ScaleType([])),
    ]
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    result = await provider._read_validator_child_keys("5Validator", "0xabc")

    assert result == []


@pytest.mark.asyncio
async def test_dispatch_subnet_routes_hyperparams() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _HyperparamsSubstrate())
    value = await provider._dispatch_subnet(
        "subnet.{netuid}.hyperparams", ["subnet", "19"], {}, 100, "0xabc"
    )
    assert isinstance(value, dict)
    assert "tempo" in value


# ---------------------------------------------------------------------------
# _read_subnet_ema_tao_flow — signed RAO -> TAO conversion
# ---------------------------------------------------------------------------


def _ema_tuple(rao_float: float, block: int = 100) -> tuple[int, dict[str, int]]:
    """Build a (u64_block, I64F64_bits) tuple as returned by SubnetEmaTaoFlow.

    Converts a RAO float to the I64F64 bits representation (multiply by 2^64)
    so unit tests can express values in natural RAO units.
    """
    bits = int(rao_float * (1 << 64))
    return (block, {"bits": bits})


class _EmaTaoFlowSubstrate:
    """Fake substrate that returns a (u64, I64F64) tuple for SubnetEmaTaoFlow.

    On-chain shape: (last_update_block: u64, ema: I64F64) where I64F64 bits
    divided by 2^64 give the value in RAO.
    """

    def __init__(self, ema_rao_float: float) -> None:
        self._value = _ema_tuple(ema_rao_float)

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        if module == "SubtensorModule" and storage_fn == "SubnetEmaTaoFlow":
            return _ScaleType(self._value)
        return _ScaleType(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ema_rao", "expected_tao"),
    [
        (float(RAO_PER_TAO * 5), 5.0),
        (float(-RAO_PER_TAO * 3), -3.0),
        (0.0, 0.0),
    ],
)
async def test_read_subnet_ema_tao_flow_preserves_sign(ema_rao: float, expected_tao: float) -> None:
    """The EMA can be negative when TAO is leaving the subnet; sign must survive
    the I64F64 bits -> RAO -> TAO conversion chain."""
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _EmaTaoFlowSubstrate(ema_rao))
    assert await provider._read_subnet_ema_tao_flow(19, "0xabc") == pytest.approx(expected_tao)


@pytest.mark.asyncio
async def test_read_subnet_ema_tao_flow_handles_none() -> None:
    """A missing storage value (None) decodes as 0.0, not an exception."""

    class _NoneSubstrate:
        async def query(
            self,
            module: str,
            storage_fn: str,
            params: list[object] | None = None,
            block_hash: str = "",
        ) -> None:
            return None

    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _NoneSubstrate())
    assert await provider._read_subnet_ema_tao_flow(19, "0xabc") == 0.0


@pytest.mark.asyncio
async def test_read_subnet_ema_tao_flow_decodes_finney_shape() -> None:
    """Verify the I64F64 bits decode against the value observed on finney block ~8133183.

    Observed: (8133183, {'bits': -62366886535159848297984346})
    Expected: -62366886535159848297984346 / 2^64 / 1e9 ≈ -0.003381 TAO
    """
    # Observed bits value from finney block ~8133183, SN1.
    observed_bits = -62366886535159848297984346

    class _FinneyShapeSubstrate:
        async def query(
            self,
            module: str,
            storage_fn: str,
            params: list[object] | None = None,
            block_hash: str = "",
        ) -> _ScaleType:
            return _ScaleType((8133183, {"bits": observed_bits}))

    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _FinneyShapeSubstrate())
    result = await provider._read_subnet_ema_tao_flow(1, "0xabc")

    expected = observed_bits / (1 << 64) / 1_000_000_000
    assert result == pytest.approx(expected, rel=1e-9)
    # Sanity: should be a small negative TAO value
    assert -1.0 < result < 0


# ---------------------------------------------------------------------------
# _wrap_substrate_exception — provider-boundary classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_cls"),
    [
        ("rate limit exceeded", RateLimitError),
        ("Too Many Requests", RateLimitError),
        ("HTTP 429: throttled", RateLimitError),
        (
            "No free-tier-eligible backend available; an API key is required",
            RateLimitError,
        ),
        ("subscribe: connection closed", SubscriptionFailedError),
        ("subscription dropped mid-stream", SubscriptionFailedError),
        ("connection refused", RPCUnreachableError),
        ("unknown method", RPCUnreachableError),
    ],
)
def test_wrap_substrate_exception_classifies_by_message(
    message: str, expected_cls: type[Exception]
) -> None:
    """Translation must happen at the provider boundary so the runtime's
    `_handle_provider_exception` ladder routes mid-poll failures correctly
    (rate-limit during a watcher's read loop must surface as provider_error,
    not internal_error).
    """
    wrapped = _wrap_substrate_exception(SubstrateRequestException(message))
    assert isinstance(wrapped, expected_cls)
    assert message in str(wrapped)
