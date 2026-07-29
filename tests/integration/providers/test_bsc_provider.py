"""BSC gas-price and transaction flows against chain-ID-56 Anvil."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import jsonschema
import pytest
import pytest_asyncio

from chainwake.providers.base import ProviderConfig
from chainwake.providers.evm import BSC_PROFILE, EvmProvider
from tests.integration.harness.evm_chain import EvmLocalChain

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

GWEI = 1_000_000_000
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "output.json"


@pytest_asyncio.fixture(scope="module")
async def bsc_chain() -> AsyncIterator[EvmLocalChain]:
    chain = EvmLocalChain(
        chain_id=BSC_PROFILE.chain_id,
        external_rpc_env="CHAINWAKE_BSC_INTEGRATION_RPC_URL",
    )
    await chain.start()
    try:
        yield chain
    finally:
        await chain.stop()


async def _provider(rpc_url: str) -> EvmProvider:
    provider = EvmProvider(BSC_PROFILE)
    await provider.connect(ProviderConfig(rpc_url=rpc_url))
    return provider


async def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return cast(
        "subprocess.CompletedProcess[str]",
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "chainwake", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        ),
    )


async def _send_transaction(chain: EvmLocalChain) -> str:
    accounts = await chain.rpc("eth_accounts")
    assert isinstance(accounts, list)
    result = await chain.rpc(
        "eth_sendTransaction",
        [
            {
                "from": str(accounts[0]),
                "to": str(accounts[1]),
                "value": "0x1",
                "gas": "0x5208",
            }
        ],
    )
    assert isinstance(result, str)
    return result


async def test_connects_to_chain_id_56_and_reads_gas_price(
    bsc_chain: EvmLocalChain,
) -> None:
    provider = await _provider(bsc_chain.rpc_url)
    try:
        observed = await provider.read_observable("network.gas-price", {})
    finally:
        await provider.disconnect()

    assert provider.profile.chain_id == 56
    assert observed.path == "network.gas-price"
    assert isinstance(observed.value, int | float)
    assert observed.value > 0


async def test_bsc_gas_price_cli_returns_schema_valid_context(
    bsc_chain: EvmLocalChain,
) -> None:
    proc = await _run_cli(
        [
            "bsc",
            "network",
            "gas-price",
            "--above",
            "0",
            "--rpc-url",
            bsc_chain.rpc_url,
            "--max-runtime",
            "5s",
        ]
    )

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["chain"] == "bsc"
    assert payload["observed"]["path"] == "network.gas-price"
    assert payload["observed"]["value"] > 0
    jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).validate(payload)


async def test_bsc_transaction_confirmation_depth(
    bsc_chain: EvmLocalChain,
) -> None:
    tx_hash = await _send_transaction(bsc_chain)
    await bsc_chain.mine(base_fee_wei=GWEI)
    pending = asyncio.create_task(
        _run_cli(
            [
                "bsc",
                "tx",
                tx_hash,
                "--confirmations",
                "2",
                "--rpc-url",
                bsc_chain.rpc_url,
                "--max-runtime",
                "5s",
            ]
        )
    )
    await asyncio.sleep(0.2)
    assert not pending.done()

    await bsc_chain.mine(base_fee_wei=GWEI)
    proc = await pending
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0, proc.stderr
    assert payload["condition"]["confirmations"] == 2
    assert payload["observed"]["confirmations"] >= 2
    assert payload["observed"]["execution_status"] == "success"


async def test_bsc_finalized_transaction_wait(
    bsc_chain: EvmLocalChain,
) -> None:
    tx_hash = await _send_transaction(bsc_chain)
    await bsc_chain.mine(base_fee_wei=GWEI)
    # Anvil implements Ethereum-style finalized-tag advancement rather than
    # BSC's fast-finality depth. Extra local blocks exercise Chainwake's
    # standard tag plumbing; BSC-specific timing is covered against mainnet.
    await bsc_chain.rpc("anvil_mine", ["0x40"])

    proc = await _run_cli(
        [
            "bsc",
            "tx",
            tx_hash,
            "--finality",
            "finalized",
            "--rpc-url",
            bsc_chain.rpc_url,
            "--max-runtime",
            "5s",
        ]
    )
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert payload["condition"]["finality"] == "finalized"
    assert payload["observed"]["finality"] == "finalized"
