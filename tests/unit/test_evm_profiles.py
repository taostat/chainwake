"""Profile-driven EVM backend contract tests."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

from chainwake.chains import backend_for
from chainwake.core.errors import RPCUnreachableError
from chainwake.core.registry import lookup
from chainwake.providers.base import BlockRef, Cadence, ProviderConfig, TxFinalityStatus
from chainwake.providers.evm import (
    BASE_PROFILE,
    BSC_PROFILE,
    ETHEREUM_PROFILE,
    EvmFeeModel,
    EvmProvider,
    EvmSubscription,
    profile_for,
)
from chainwake.providers.market import MarketPriceFeed, MarketPriceSource, TokenInfo, UsdPrice

pytestmark = pytest.mark.unit

_TX_HASH = f"0x{'12' * 32}"
_INCLUSION_HASH = f"0x{'34' * 32}"
_HEAD_HASH = f"0x{'56' * 32}"
_RECEIPT = {
    "transactionHash": _TX_HASH,
    "blockNumber": "0x64",
    "blockHash": _INCLUSION_HASH,
    "status": "0x1",
    "gasUsed": "0x5208",
    "effectiveGasPrice": "0x2faf080",
}
_INCLUSION_BLOCK = {
    "number": "0x64",
    "hash": _INCLUSION_HASH,
    "timestamp": "0x65ec8780",
    "baseFeePerGas": "0x0",
}
_HEAD_BLOCK = {
    "number": "0x65",
    "hash": _HEAD_HASH,
    "timestamp": "0x65ec8781",
    "baseFeePerGas": "0x0",
}


def test_builtin_evm_profiles_are_the_single_chain_capability_source() -> None:
    assert ETHEREUM_PROFILE.chain_id == 1
    assert ETHEREUM_PROFILE.block_seconds == 12.0
    assert ETHEREUM_PROFILE.fee_model == EvmFeeModel.EIP1559

    assert BASE_PROFILE.chain_id == 8453
    assert BASE_PROFILE.block_seconds == 2.0
    assert BASE_PROFILE.supported_finality_levels == ("included", "safe", "finalized")
    assert BASE_PROFILE.fee_model == EvmFeeModel.OP_STACK

    assert BSC_PROFILE.chain_id == 56
    assert BSC_PROFILE.block_seconds == 0.45
    assert BSC_PROFILE.supported_finality_levels == ("included", "finalized")
    assert BSC_PROFILE.fee_model == EvmFeeModel.GAS_PRICE

    for profile in (ETHEREUM_PROFILE, BASE_PROFILE, BSC_PROFILE):
        assert profile.default_rpc.startswith("wss://")
        assert profile.coingecko_platform
        assert EvmSubscription.NEW_HEADS in profile.subscription_capabilities
        assert profile_for(profile.alias) is profile


def test_evm_backends_are_constructed_from_their_profiles() -> None:
    for profile in (ETHEREUM_PROFILE, BASE_PROFILE, BSC_PROFILE):
        backend = backend_for(profile.alias)
        provider = backend.create_provider()

        assert backend.runtime.block_seconds == profile.block_seconds
        assert isinstance(provider, EvmProvider)
        assert provider.profile is profile
        assert provider.short_alias == profile.alias


@pytest.mark.asyncio
async def test_connect_rejects_an_rpc_for_the_wrong_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BASE_PROFILE)
    monkeypatch.setattr(provider, "_open_primary", AsyncMock())
    monkeypatch.setattr(provider, "_rpc", AsyncMock(return_value="0x1"))
    monkeypatch.setattr(provider, "_drop_primary", AsyncMock())

    with pytest.raises(RPCUnreachableError, match=r"Base RPC.*chain ID 1.*expected 8453"):
        await provider.connect(ProviderConfig(rpc_url="wss://wrong-chain.test"))


def test_profile_drives_chain_specific_fee_catalogues() -> None:
    assert lookup("network.base-fee", chain="base").natural_cadence == Cadence.PER_BLOCK
    assert lookup("network.l1-base-fee", chain="base").read_cost == 2
    assert lookup("network.l1-blob-base-fee", chain="base").read_cost == 2
    with pytest.raises(KeyError):
        lookup("network.gas-price", chain="base")

    assert lookup("network.gas-price", chain="bsc").natural_cadence == Cadence.PER_BLOCK
    with pytest.raises(KeyError):
        lookup("network.base-fee", chain="bsc")

    for chain in ("eth", "base", "bsc"):
        token_price = lookup("token.{token}.price", chain=chain)
        assert token_price.natural_cadence == Cadence.OTHER
        assert token_price.observation_policy.default_poll_seconds == 60.0
        assert token_price.subscription_supported is False


@pytest.mark.asyncio
async def test_token_price_is_chain_scoped_and_carries_explicit_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TokenInfo(
        chain_id=1,
        address="0x6b175474e89094c44da98b954eedeac495271d0f",
        name="Dai",
        symbol="DAI",
        decimals=18,
    )
    source = AsyncMock()
    source.resolve_token.return_value = token
    source.price_usd.return_value = UsdPrice(value=0.9998, last_updated_at=1_722_000_000)
    provider = EvmProvider(
        ETHEREUM_PROFILE,
        market_prices=MarketPriceFeed(cast("MarketPriceSource", source)),
    )
    monkeypatch.setattr(provider, "_rpc", AsyncMock(return_value=_HEAD_BLOCK))

    observed = await provider.read_observable("token.DAI.price", {})

    assert observed.value == pytest.approx(0.9998)
    assert observed.path == "token.DAI.price"
    assert observed.meta == {
        "source": "coingecko",
        "quote_currency": "usd",
        "token_address": token.address,
        "token_name": "Dai",
        "token_symbol": "DAI",
        "token_decimals": 18,
        "price_last_updated_at": "2024-07-26T13:20:00Z",
    }
    source.resolve_token.assert_awaited_once_with(
        platform="ethereum",
        chain_id=1,
        identifier="DAI",
    )


@pytest.mark.asyncio
async def test_bsc_gas_price_uses_eth_gas_price_and_pinned_head_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BSC_PROFILE)
    rpc = AsyncMock(side_effect=["0x2faf080", _HEAD_BLOCK])
    monkeypatch.setattr(provider, "_rpc", rpc)

    observed = await provider.read_observable(
        "network.gas-price",
        {},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    assert observed.value == pytest.approx(0.05)
    assert observed.meta == {"gas_price_wei": 50_000_000, "unit": "gwei"}
    assert rpc.await_args_list[0].args == ("eth_gasPrice", [])
    assert rpc.await_args_list[1].args == ("eth_getBlockByNumber", ["0x65", False])


@pytest.mark.asyncio
async def test_base_l1_base_fee_reads_the_gas_oracle_at_the_pinned_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BASE_PROFILE)
    rpc = AsyncMock(side_effect=[_HEAD_BLOCK, hex(20_000_000_000)])
    monkeypatch.setattr(provider, "_rpc", rpc)

    observed = await provider.read_observable(
        "network.l1-base-fee",
        {},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    assert observed.value == pytest.approx(20.0)
    call, block_tag = rpc.await_args_list[1].args[1]
    assert rpc.await_args_list[1].args[0] == "eth_call"
    assert call["to"] == "0x420000000000000000000000000000000000000F"
    assert call["data"] == "0x519b4bd3"
    assert block_tag == {"blockHash": _HEAD_HASH, "requireCanonical": True}
    assert observed.meta == {"l1_base_fee_wei": 20_000_000_000, "unit": "gwei"}


@pytest.mark.asyncio
async def test_base_l1_blob_base_fee_reads_the_gas_oracle_at_the_pinned_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BASE_PROFILE)
    rpc = AsyncMock(side_effect=[_HEAD_BLOCK, hex(2_000_000_000)])
    monkeypatch.setattr(provider, "_rpc", rpc)

    observed = await provider.read_observable(
        "network.l1-blob-base-fee",
        {},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    call, block_tag = rpc.await_args_list[1].args[1]
    assert call["data"] == "0xf8206140"
    assert block_tag == {"blockHash": _HEAD_HASH, "requireCanonical": True}
    assert observed.value == pytest.approx(2.0)
    assert observed.meta == {
        "l1_blob_base_fee_wei": 2_000_000_000,
        "unit": "gwei",
    }


@pytest.mark.asyncio
async def test_base_safe_head_promotes_a_canonical_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BASE_PROFILE)

    async def rpc(method: str, params: list[object]) -> object:
        if method == "eth_getTransactionReceipt":
            return _RECEIPT
        [block, _full] = params
        if block == "0x64":
            return _INCLUSION_BLOCK
        if block == "0x65":
            return _HEAD_BLOCK
        if block == "safe":
            return _HEAD_BLOCK
        raise AssertionError((method, params))

    monkeypatch.setattr(provider, "_rpc", AsyncMock(side_effect=rpc))

    observed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "safe"},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    status = observed.value
    assert isinstance(status, TxFinalityStatus)
    assert status.level == "safe"


@pytest.mark.asyncio
async def test_bsc_finalized_head_and_confirmation_waits_share_receipt_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BSC_PROFILE)

    async def rpc(method: str, params: list[object]) -> object:
        if method == "eth_getTransactionReceipt":
            return _RECEIPT
        [block, _full] = params
        if block == "0x64":
            return _INCLUSION_BLOCK
        if block == "0x65":
            return _HEAD_BLOCK
        if block == "finalized":
            return _HEAD_BLOCK
        raise AssertionError((method, params))

    monkeypatch.setattr(provider, "_rpc", AsyncMock(side_effect=rpc))

    confirmed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "included", "confirmations": 2},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )
    finalized = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "finalized"},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    confirmed_status = confirmed.value
    finalized_status = finalized.value
    assert isinstance(confirmed_status, TxFinalityStatus)
    assert isinstance(finalized_status, TxFinalityStatus)
    assert confirmed_status.confirmations == 2
    assert finalized_status.level == "finalized"


@pytest.mark.asyncio
async def test_bsc_provider_rejects_safe_without_any_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EvmProvider(BSC_PROFILE)
    rpc = AsyncMock()
    monkeypatch.setattr(provider, "_rpc", rpc)

    with pytest.raises(NotImplementedError, match="BSC does not support 'safe' finality"):
        await provider.read_observable(f"tx.{_TX_HASH}", {"finality": "safe"})

    rpc.assert_not_awaited()
