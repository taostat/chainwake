"""Ethereum base-fee vertical slice against a disposable Anvil node."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import jsonschema
import pytest
import pytest_asyncio

from chainwake.providers.base import BlockRef, ProviderConfig
from chainwake.providers.evm import ETHEREUM_PROFILE, EvmProvider
from tests.integration.harness.evm_chain import EvmLocalChain

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "output.json"
GWEI = 1_000_000_000
DAI_ADDRESS = "0x6b175474e89094c44da98b954eedeac495271d0f"


@pytest_asyncio.fixture(scope="module")
async def ethereum_chain() -> AsyncIterator[EvmLocalChain]:
    chain = EvmLocalChain(
        chain_id=ETHEREUM_PROFILE.chain_id,
        external_rpc_env="CHAINWAKE_ETH_INTEGRATION_RPC_URL",
    )
    await chain.start()
    try:
        yield chain
    finally:
        await chain.stop()


@pytest.fixture
def coin_gecko_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Serve deterministic token identity and price responses to the CLI subprocess."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/ethereum/all.json":
                payload: object = {
                    "tokens": [
                        {
                            "chainId": 1,
                            "address": DAI_ADDRESS,
                            "name": "Dai",
                            "symbol": "DAI",
                            "decimals": 18,
                        }
                    ]
                }
            elif self.path.startswith("/simple/token_price/ethereum?"):
                payload = {
                    DAI_ADDRESS: {
                        "usd": 1.001,
                        "last_updated_at": 1_722_000_000,
                    }
                }
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("CHAINWAKE_COINGECKO_TOKEN_LIST_BASE_URL", base_url)
    monkeypatch.setenv("CHAINWAKE_COINGECKO_API_BASE_URL", base_url)
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def _provider(rpc_url: str) -> EvmProvider:
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url=rpc_url))
    return provider


async def _run_cli(
    args: list[str],
    *,
    subprocess_timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    return cast(
        "subprocess.CompletedProcess[str]",
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "chainwake", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=subprocess_timeout,
            env=env,
        ),
    )


async def _send_transaction(
    chain: EvmLocalChain,
    *,
    to: str | None = None,
    data: str | None = None,
) -> str:
    accounts = await chain.rpc("eth_accounts")
    assert isinstance(accounts, list)
    sender = str(accounts[0])
    transaction: dict[str, object] = {
        "from": sender,
        "to": to or str(accounts[1]),
        "value": "0x1",
        "gas": "0x186a0",
    }
    if data is not None:
        transaction["data"] = data
    result = await chain.rpc("eth_sendTransaction", [transaction])
    assert isinstance(result, str)
    return result


async def test_reads_latest_base_fee_in_gwei(
    ethereum_chain: EvmLocalChain,
) -> None:
    """The provider connects to Anvil and reports latest base fee in gwei."""

    expected = await ethereum_chain.mine(base_fee_wei=1_500_000_000)
    provider = await _provider(ethereum_chain.rpc_url)
    try:
        observed = await provider.read_observable("network.base-fee", {})
    finally:
        await provider.disconnect()

    assert observed.path == "network.base-fee"
    assert isinstance(observed.value, int | float)
    assert observed.value == 1.5
    assert observed.value >= 0
    assert observed.block == expected.number
    assert observed.block_hash == expected.hash
    assert observed.timestamp.tzinfo is not None


async def test_new_head_can_be_read_at_its_exact_hash(
    ethereum_chain: EvmLocalChain,
) -> None:
    """A ``newHeads`` item is sufficient for an exact, pinned base-fee read."""

    provider = await _provider(ethereum_chain.rpc_url)
    head_stream = cast("AsyncGenerator[BlockRef]", provider.subscribe_heads())
    pending_head = asyncio.create_task(anext(head_stream))
    try:
        # Let the async iterator establish its dedicated subscription before
        # mining; Anvil has interval and automining disabled.
        await asyncio.sleep(0.1)
        expected = await ethereum_chain.mine(base_fee_wei=2_500_000_000)
        head = await asyncio.wait_for(pending_head, timeout=5.0)
        observed = await provider.read_observable(
            "network.base-fee",
            {},
            at_block=head,
        )
    finally:
        if not pending_head.done():
            pending_head.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_head
        await head_stream.aclose()
        await provider.disconnect()

    assert head == BlockRef(number=expected.number, hash=expected.hash)
    assert observed.value == 2.5
    assert observed.block == head.number
    assert observed.block_hash == head.hash


async def test_cli_matches_deterministic_base_fee(
    ethereum_chain: EvmLocalChain,
) -> None:
    """``eth network base-fee`` exits with schema-valid matched context."""

    expected = await ethereum_chain.mine(base_fee_wei=7 * GWEI)
    proc = await _run_cli(
        [
            "eth",
            "network",
            "base-fee",
            "--below",
            "8",
            "--rpc-url",
            ethereum_chain.rpc_url,
            "--max-runtime",
            "5s",
        ]
    )

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["chain"] == "eth"
    assert payload["watcher"]["resource"] == "network"
    assert payload["watcher"]["resource_id"] is None
    assert payload["watcher"]["primitive"] == "threshold"
    assert payload["condition"]["operator"] == "below"
    assert payload["condition"]["target"] == 8.0
    assert payload["observed"]["path"] == "network.base-fee"
    assert payload["observed"]["value"] == 7.0
    assert payload["observed"]["block"] == expected.number
    assert payload["observed"]["block_hash"] == expected.hash
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


async def test_cli_resolves_token_and_returns_price_provenance(
    ethereum_chain: EvmLocalChain,
    coin_gecko_server: None,
) -> None:
    """``eth token DAI price`` covers resolver, quote, chain anchor, and output."""

    proc = await _run_cli(
        [
            "eth",
            "token",
            "DAI",
            "price",
            "--above",
            "1",
            "--rpc-url",
            ethereum_chain.rpc_url,
            "--max-runtime",
            "5s",
        ]
    )

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["resource"] == "token"
    assert payload["watcher"]["resource_id"] == "DAI"
    assert payload["observed"]["path"] == "token.DAI.price"
    assert payload["observed"]["value"] == 1.001
    assert payload["observed"]["meta"] == {
        "source": "coingecko",
        "quote_currency": "usd",
        "token_address": DAI_ADDRESS,
        "token_name": "Dai",
        "token_symbol": "DAI",
        "token_decimals": 18,
        "price_last_updated_at": "2024-07-26T13:20:00Z",
    }
    jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).validate(payload)


async def test_cli_waits_for_successful_transaction_confirmation(
    ethereum_chain: EvmLocalChain,
) -> None:
    tx_hash = await _send_transaction(ethereum_chain)
    pending = asyncio.create_task(
        _run_cli(
            [
                "eth",
                "tx",
                tx_hash,
                "--rpc-url",
                ethereum_chain.rpc_url,
                "--max-runtime",
                "5s",
            ]
        )
    )
    await asyncio.sleep(0.2)
    await ethereum_chain.mine(base_fee_wei=GWEI)

    proc = await pending

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["watcher"]["chain"] == "eth"
    assert payload["watcher"]["resource"] == "tx"
    assert payload["condition"] == {
        "finality": "included",
        "confirmations": 1,
        "timeout": None,
    }
    assert payload["observed"]["tx_hash"] == tx_hash
    assert payload["observed"]["finality"] == "included"
    assert payload["observed"]["confirmations"] >= 1
    assert payload["observed"]["execution_status"] == "success"
    assert payload["observed"]["gas_used"] == 21_000
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


async def test_cli_waits_for_requested_confirmation_depth(
    ethereum_chain: EvmLocalChain,
) -> None:
    tx_hash = await _send_transaction(ethereum_chain)
    await ethereum_chain.mine(base_fee_wei=GWEI)
    pending = asyncio.create_task(
        _run_cli(
            [
                "eth",
                "tx",
                tx_hash,
                "--confirmations",
                "2",
                "--rpc-url",
                ethereum_chain.rpc_url,
                "--max-runtime",
                "5s",
            ]
        )
    )
    await asyncio.sleep(0.2)
    assert not pending.done()

    await ethereum_chain.mine(base_fee_wei=GWEI)
    proc = await pending
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0, proc.stderr
    assert payload["condition"]["confirmations"] == 2
    assert payload["observed"]["confirmations"] >= 2


async def test_cli_reports_reverted_transaction_receipt(
    ethereum_chain: EvmLocalChain,
) -> None:
    accounts = await ethereum_chain.rpc("eth_accounts")
    assert isinstance(accounts, list)
    reverter = "0x0000000000000000000000000000000000000bad"
    await ethereum_chain.rpc("anvil_setCode", [reverter, "0x60006000fd"])
    tx_hash = await _send_transaction(ethereum_chain, to=reverter)
    await ethereum_chain.mine(base_fee_wei=GWEI)

    proc = await _run_cli(
        [
            "eth",
            "tx",
            tx_hash,
            "--rpc-url",
            ethereum_chain.rpc_url,
            "--max-runtime",
            "5s",
        ]
    )
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0, proc.stderr
    assert payload["observed"]["execution_status"] == "reverted"
