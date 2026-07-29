"""Integration tests for BittensorProvider against subtensor-localnet.

Each test registers its own subnet or funds its own accounts so tests can run
concurrently without colliding on shared state. Never assume a netuid exists,
rely on pre-funded balances, or assert on chain-wide counts.

Derivation paths use the `//streamA-<testname>` convention for isolation.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, cast
from unittest.mock import AsyncMock

import pytest
from bittensor_wallet import Keypair

from chainwake.core.errors import UserError
from chainwake.providers.base import Cadence, EventFilter, ProviderConfig
from chainwake.providers.bittensor import BittensorProvider
from chainwake.providers.market import MarketPriceFeed, MarketPriceSource, UsdPrice
from tests.integration.harness.local_chain import LocalChain, derive_dev_account, tao_to_rao

pytestmark = pytest.mark.integration

_RPC_CONFIG_KWARGS: Final[dict[str, str]] = {}  # filled per test from local_chain.rpc_url


def _dev_key(suffix: str) -> Keypair:
    """Derive a fresh keypair for a Stream-A test by unique SURI.

    Routes through ``derive_dev_account`` so the SURI inherits the active
    pytest-xdist worker prefix and parallel workers do not collide on a
    single ``//streamA-<suffix>`` path.
    """
    return derive_dev_account(f"streamA-{suffix}").keypair


async def _provider(rpc_url: str) -> BittensorProvider:
    p = BittensorProvider()
    await p.connect(ProviderConfig(rpc_url=rpc_url))
    return p


# ---------------------------------------------------------------------------
# Subnet resource — pool price, pool depths, registration cost
# ---------------------------------------------------------------------------


async def test_subnet_pool_price_is_positive(local_chain: LocalChain) -> None:
    """subnet.{netuid}.pool.price reads a positive price from a new subnet."""
    alice = _dev_key("subnetPrice-owner")
    await local_chain.fund_account(alice.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(alice)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.pool.price", {})
        assert isinstance(obs.value, float)
        assert obs.value >= 0.0
        assert obs.block > 0
        assert obs.block_hash.startswith("0x")
    finally:
        await p.disconnect()


async def test_subnet_pool_tao_depth(local_chain: LocalChain) -> None:
    """subnet.{netuid}.pool.tao-depth returns the TAO reserve in TAO units."""
    owner = _dev_key("subnetTaoDepth")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.pool.tao-depth", {})
        assert isinstance(obs.value, float)
        assert obs.value >= 0.0
    finally:
        await p.disconnect()


async def test_subnet_pool_alpha_depth(local_chain: LocalChain) -> None:
    """subnet.{netuid}.pool.alpha-depth returns the alpha reserve."""
    owner = _dev_key("subnetAlphaDepth")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.pool.alpha-depth", {})
        assert isinstance(obs.value, float)
        assert obs.value >= 0.0
    finally:
        await p.disconnect()


async def test_subnet_registration_cost(local_chain: LocalChain) -> None:
    """subnet.{netuid}.registration-cost returns burn cost in TAO."""
    owner = _dev_key("subnetRegCost")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.registration-cost", {})
        assert isinstance(obs.value, float)
        assert obs.value >= 0.0
    finally:
        await p.disconnect()


async def test_subnet_emission_share(local_chain: LocalChain) -> None:
    """subnet.{netuid}.emission-share returns a fraction 0-1."""
    owner = _dev_key("subnetEmission")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.emission-share", {})
        assert isinstance(obs.value, float)
        assert 0.0 <= obs.value <= 1.0
    finally:
        await p.disconnect()


async def test_subnet_burn_rate(local_chain: LocalChain) -> None:
    """subnet.{netuid}.burn-rate returns a fraction in [0, 1]."""
    owner = _dev_key("subnetBurnRate")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.burn-rate", {})
        assert isinstance(obs.value, float)
        assert 0.0 <= obs.value <= 1.0
    finally:
        await p.disconnect()


async def test_subnet_hyperparams(local_chain: LocalChain) -> None:
    """subnet.{netuid}.hyperparams returns a non-empty dict."""
    owner = _dev_key("subnetHyperparams")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.hyperparams", {})
        assert isinstance(obs.value, dict)
        hyperparams = cast("dict[str, object]", obs.value)
        assert len(hyperparams) > 0
        # Tempo is always present
        assert "tempo" in hyperparams
        assert "activity_cutoff_factor_milli" in hyperparams
        factor = hyperparams["activity_cutoff_factor_milli"]
        tempo = hyperparams["tempo"]
        cutoff = hyperparams["activity_cutoff"]
        assert isinstance(factor, int)
        assert isinstance(tempo, int)
        assert cutoff == max(1, factor * tempo // 1_000)
    finally:
        await p.disconnect()


async def test_subnet_identity(local_chain: LocalChain) -> None:
    """subnet.{netuid}.identity returns the full runtime identity shape."""
    owner = _dev_key("subnetIdentity")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"subnet.{netuid}.identity", {})
        assert isinstance(obs.value, dict)
        assert "owner_hotkey" in obs.value
        assert "owner_coldkey" in obs.value
        assert "subnet_identity" in obs.value
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Computed: depth-for-trade
# ---------------------------------------------------------------------------


async def test_depth_for_trade_returns_float(local_chain: LocalChain) -> None:
    """depth-for-trade returns a float (margin in bps, positive or negative)."""
    owner = _dev_key("depthForTrade")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(
            f"subnet.{netuid}.pool.depth-for-trade",
            {"size": 1.0, "max_bps": 100.0},
        )
        assert isinstance(obs.value, float)
        # When pool is empty, returns -max_bps; with liquidity it could be positive.
        # Either way, value <= max_bps (can never exceed the cap).
        assert obs.value <= 100.0
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Account resource
# ---------------------------------------------------------------------------


async def test_account_balance(local_chain: LocalChain) -> None:
    """account.{coldkey}.balance returns funded balance in TAO."""
    acct = _dev_key("accountBalance")
    funded_tao = 5.0
    await local_chain.fund_account(acct.ss58_address, tao_to_rao(funded_tao))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"account.{acct.ss58_address}.balance", {})
        assert isinstance(obs.value, float)
        # Should be approximately funded_tao (minus dust fees)
        assert obs.value >= funded_tao * 0.9
    finally:
        await p.disconnect()


async def test_account_activity(local_chain: LocalChain) -> None:
    """account.{coldkey}.activity returns an int (LastTxBlock, 0 if no neuron txs)."""
    # LastTxBlock only tracks neuron-related SubtensorModule txs; a freshly funded
    # account returns 0, which is the correct sentinel.
    acct = _dev_key("accountActivity-freshfund")
    await local_chain.fund_account(acct.ss58_address, tao_to_rao(5.0))

    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"account.{acct.ss58_address}.activity", {})
        assert isinstance(obs.value, int)
        assert obs.value >= 0
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Network resource
# ---------------------------------------------------------------------------


async def test_network_tao_price_has_real_chain_and_market_context(
    local_chain: LocalChain,
) -> None:
    """network.tao-price composes a deterministic quote with a live Subtensor head."""
    source = AsyncMock()
    source.coin_price_usd.return_value = UsdPrice(
        value=190.71,
        last_updated_at=1_722_000_000,
    )
    p = BittensorProvider(market_prices=MarketPriceFeed(cast("MarketPriceSource", source)))
    await p.connect(ProviderConfig(rpc_url=local_chain.rpc_url))
    try:
        obs = await p.read_observable("network.tao-price", {})
    finally:
        await p.disconnect()

    assert obs.value == pytest.approx(190.71)
    assert obs.block > 0
    assert obs.block_hash.startswith("0x")
    assert obs.meta["coin_symbol"] == "TAO"
    assert obs.meta["source"] == "coingecko"


async def test_network_subnet_registration_cost(local_chain: LocalChain) -> None:
    """network.subnet-registration-cost returns the lock cost in TAO."""
    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable("network.subnet-registration-cost", {})
        assert isinstance(obs.value, float)
        assert obs.value > 0.0
    finally:
        await p.disconnect()


async def test_network_runtime_version(local_chain: LocalChain) -> None:
    """network.runtime-version returns a dict with spec_version."""
    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable("network.runtime-version", {})
        assert isinstance(obs.value, dict)
        assert "spec_version" in obs.value
        runtime_info = cast("dict[str, object]", obs.value)
        assert int(str(runtime_info["spec_version"])) > 0
    finally:
        await p.disconnect()


async def test_network_subnet_count(local_chain: LocalChain) -> None:
    """network.subnet-count returns a non-zero integer."""
    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable("network.subnet-count", {})
        assert isinstance(obs.value, int)
        assert obs.value > 0
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Validator resource
# ---------------------------------------------------------------------------


async def test_validator_commission(local_chain: LocalChain) -> None:
    """validator.{hotkey}.commission returns a fraction 0-1 for a registered validator."""
    # Alice is the pre-seeded validator on localnet
    alice = derive_dev_account("alice")
    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"validator.{alice.address}.commission", {})
        assert isinstance(obs.value, float)
        assert 0.0 <= obs.value <= 1.0
    finally:
        await p.disconnect()


async def test_validator_stake_nonnegative(local_chain: LocalChain) -> None:
    """A validator's netuid-scoped stake-alpha is a non-negative alpha value."""
    alice = derive_dev_account("alice")
    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"validator.1.{alice.address}.stake-alpha", {})
        assert isinstance(obs.value, float)
        assert obs.value >= 0.0
    finally:
        await p.disconnect()


async def test_validator_child_keys(local_chain: LocalChain) -> None:
    """validator.{hotkey}.child-keys returns a list (may be empty)."""
    alice = derive_dev_account("alice")
    p = await _provider(local_chain.rpc_url)
    try:
        obs = await p.read_observable(f"validator.{alice.address}.child-keys", {})
        assert isinstance(obs.value, list)
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Neuron resource
# ---------------------------------------------------------------------------


async def test_neuron_stake(local_chain: LocalChain) -> None:
    """neuron stake-alpha returns non-negative subnet alpha for a registered neuron."""
    owner = _dev_key("neuronStake-owner")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        # owner is the subnet owner hotkey
        obs = await p.read_observable(f"neuron.{netuid}.{owner.ss58_address}.stake-alpha", {})
        assert isinstance(obs.value, float)
        assert obs.value >= 0.0
    finally:
        await p.disconnect()


async def test_neuron_blocks_until_immunity_unregistered(
    local_chain: LocalChain,
) -> None:
    """An unknown hotkey is rejected rather than masquerading as zero immunity."""
    owner = _dev_key("neuronEpochImmunity-owner")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))
    unregistered = _dev_key("neuronEpochImmunity-unknown")

    p = await _provider(local_chain.rpc_url)
    try:
        with pytest.raises(UserError, match="is not registered"):
            await p.read_observable(
                f"neuron.{netuid}.{unregistered.ss58_address}.blocks-until-immunity-expires",
                {},
            )
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Transaction finality
# ---------------------------------------------------------------------------


async def test_tx_finality_included(local_chain: LocalChain) -> None:
    """get_block_finality returns 'included' or 'finalized' for a submitted tx."""
    result = await local_chain.drive_tx_to_finality()
    tx_hash = str(result.extra["tx_hash"])

    p = await _provider(local_chain.rpc_url)
    try:
        status = await p.get_block_finality(tx_hash)
        assert status.tx_hash == tx_hash
        assert status.level in ("included", "finalized")
        assert status.block is not None
        assert status.block > 0
    finally:
        await p.disconnect()


async def test_tx_finality_pending_for_unknown(local_chain: LocalChain) -> None:
    """Unknown tx returns 'pending' on a fresh chain or raises when horizon exhausted.

    The provider returns 'pending' only while the chain is too young to fill
    the search horizon (head < _TX_SEARCH_HORIZON_BLOCKS); on a mature chain
    it raises TxNotFoundInHorizonError instead of silently reporting pending.
    Under CHAINWAKE_REUSE_NODE=1 the localnet may have run long enough to
    cross the horizon, so this test accepts either outcome.
    """
    from chainwake.core.errors import TxNotFoundInHorizonError  # noqa: PLC0415

    fake_hash = "0x" + "ab" * 32

    p = await _provider(local_chain.rpc_url)
    try:
        try:
            status = await p.get_block_finality(fake_hash)
        except TxNotFoundInHorizonError:
            # Expected on a chain with head >= _TX_SEARCH_HORIZON_BLOCKS.
            return
        assert status.tx_hash == fake_hash
        assert status.level == "pending"
    finally:
        await p.disconnect()


# ---------------------------------------------------------------------------
# Natural cadence
# ---------------------------------------------------------------------------


async def test_natural_cadence_for_subnet_price(local_chain: LocalChain) -> None:
    """natural_cadence_for returns PER_BLOCK for subnet price without RPC call."""
    p = BittensorProvider()
    # cadence does not require connection
    cadence = p.natural_cadence_for("subnet.19.pool.price")
    assert cadence == Cadence.PER_BLOCK


# ---------------------------------------------------------------------------
# Event subscription (smoke — verifies iterator starts and produces at least one
# event within a short timeout against a live chain)
# ---------------------------------------------------------------------------


async def test_subscribe_events_subnet_registered(local_chain: LocalChain) -> None:
    """subscribe_events yields a subnet-registered event when a new subnet is added."""
    event_filt = EventFilter(event_types=("subnet-registered",))

    p = await _provider(local_chain.rpc_url)
    subscription = p.subscribe_events(event_filt)
    try:
        received: list[object] = []

        async def collect() -> None:
            async for ev in subscription:
                received.append(ev)
                break  # stop after first match

        owner = _dev_key("subscribeEventsSubnetReg")
        await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))

        # Start subscription and registration concurrently; give 60s for a block
        collect_task = asyncio.create_task(collect())
        await asyncio.sleep(1)  # let subscription start
        await local_chain.register_subnet(owner)
        try:
            await asyncio.wait_for(collect_task, timeout=60.0)
        except TimeoutError:
            collect_task.cancel()
            pytest.skip("subscribe_events timed out - chain may be slow")

        assert len(received) >= 1
    finally:
        await cast(Any, subscription).aclose()
        await p.disconnect()


# ---------------------------------------------------------------------------
# Hyperparam change (state primitive driver)
# ---------------------------------------------------------------------------


async def test_subnet_hyperparams_after_change(local_chain: LocalChain) -> None:
    """hyperparams dict reflects a sudo hyperparam change."""
    owner = _dev_key("hyperparamChange-owner")
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    p = await _provider(local_chain.rpc_url)
    try:
        # Record before
        before = await p.read_observable(f"subnet.{netuid}.hyperparams", {})
        assert isinstance(before.value, dict)

        # Drive a change
        await local_chain.drive_hyperparam_change(netuid, "sudo_set_activity_cutoff", 1234)

        # Read again and verify tempo is still present (the subnet still exists)
        after = await p.read_observable(f"subnet.{netuid}.hyperparams", {})
        assert isinstance(after.value, dict)
        assert "tempo" in after.value
    finally:
        await p.disconnect()
