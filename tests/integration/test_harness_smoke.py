"""Harness smoke test: boot localnet, read head block, tear down."""

from __future__ import annotations

import pytest

from tests.integration.harness.local_chain import LocalChain
from tests.integration.harness.protocol import ChainTestHarness

pytestmark = pytest.mark.integration


async def test_harness_protocol_membership() -> None:
    chain = LocalChain()
    assert isinstance(chain, ChainTestHarness)


async def test_localnet_produces_blocks(local_chain: LocalChain) -> None:
    head_a = await local_chain.head()
    assert head_a > 0


async def test_subnet_registration_driver(local_chain: LocalChain) -> None:
    result = await local_chain.drive_subnet_registration()
    assert result.block > 0
    assert "netuid" in result.extra
    netuid = result.extra["netuid"]
    assert isinstance(netuid, int)
    assert netuid >= 0


async def test_tx_finality_driver(local_chain: LocalChain) -> None:
    result = await local_chain.drive_tx_to_finality()
    assert result.block > 0
    assert "tx_hash" in result.extra
