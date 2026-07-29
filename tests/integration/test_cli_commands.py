"""Integration tests for the resource-first CLI surface.

Spawns `chainwake` as a subprocess against subtensor-localnet. Each test
registers its own subnet (never hardcodes a netuid) and uses a unique
derivation path (//TestN) so concurrent integration runs share the same
node without collision.

Isolation rules:
  - Never assume a specific netuid exists; register one fresh per test.
  - Never assert on chain-wide counts.
  - Use //TestN derivation paths unique per test.
  - Sudo operations are shared state; keep them minimal.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import jsonschema
import pytest

from tests.integration.harness.local_chain import LocalChain, derive_dev_account

pytestmark = pytest.mark.integration

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "output.json"


async def _run_cli(
    args: list[str],
    *,
    subprocess_timeout: float = 30.0,
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


def _validate_schema(payload: dict) -> None:  # type: ignore[type-arg]
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


# ---------------------------------------------------------------------------
# bt subnet <netuid> price — threshold (resource-first form)
# ---------------------------------------------------------------------------


async def test_subnet_price_below_matches(local_chain: LocalChain) -> None:
    """bt subnet <netuid> price --below fires when price is below threshold."""
    # Register a fresh subnet so we never depend on a specific netuid.
    owner = derive_dev_account("test1").keypair
    await local_chain.fund_account(owner.ss58_address, 10_000_000_000_000)
    result = await local_chain.drive_subnet_registration()
    netuid = result.extra["netuid"]

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--below",
            "999999",  # virtually guaranteed to fire on any subnet
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "15s",
        ],
        subprocess_timeout=30.0,
    )

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["chain"] == "bt"
    assert payload["watcher"]["resource"] == "subnet"
    assert payload["watcher"]["resource_id"] == str(netuid)
    assert payload["watcher"]["primitive"] == "threshold"
    assert payload["condition"]["operator"] == "below"
    assert payload["condition"]["target"] == 999999.0
    assert payload["observed"]["value"] < 999999.0
    assert payload["observed"]["block"] > 0
    _validate_schema(payload)


async def test_subnet_price_above_timeout(local_chain: LocalChain) -> None:
    """bt subnet <netuid> price --above with very high target times out."""
    owner = derive_dev_account("test2").keypair
    await local_chain.fund_account(owner.ss58_address, 10_000_000_000_000)
    result = await local_chain.drive_subnet_registration()
    netuid = result.extra["netuid"]

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--above",
            "999999999",  # virtually impossible to trigger
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "3s",
        ],
        subprocess_timeout=15.0,
    )

    assert proc.returncode == 1, (
        f"expected exit 1 (timeout), got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "timeout"
    assert payload["reason"] == "max_runtime_reached"
    assert payload["observed"] is None
    _validate_schema(payload)


async def test_subnet_price_provider_error_on_bad_rpc(local_chain: LocalChain) -> None:
    """Using a bad RPC URL exits 3 with a provider_error payload."""
    # No need to register a subnet — the bad RPC fails before any chain query.
    # Use netuid=1 which is always allocated on subtensor-localnet genesis.
    del local_chain  # fixture required to ensure the node is running

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            "1",
            "price",
            "--below",
            "999999",
            "--rpc-url",
            "ws://127.0.0.1:19999",  # nothing listening here
            "--max-runtime",
            "3s",
        ],
        subprocess_timeout=15.0,
    )

    assert proc.returncode == 3, (
        f"expected exit 3 (provider_error), got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "provider_error"
    assert payload["reason"] == "rpc_unreachable"
    _validate_schema(payload)
