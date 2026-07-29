"""Integration tests for runtime budget enforcement.

Tests:
  - --max-runtime expiry → timeout payload (exit code 1)
  - --max-ru exhaustion → budget_exhausted payload (exit code 1)

Isolation rules:
  - Use register_subnet for fresh netuids.
  - Use unique derivation paths (//streamF-<test-name>).
  - Never assert on chain-wide counts.
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

from tests.integration.harness.local_chain import LocalChain, derive_dev_account, tao_to_rao

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


def _validate_schema(payload: dict[str, object]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


async def test_max_runtime_expiry_emits_timeout(local_chain: LocalChain) -> None:
    """--max-runtime-seconds expiry produces a schema-valid timeout payload."""
    # Register a fresh subnet so this never races with any other worker
    # exercising subnet 1.  The threshold itself is impossible.
    owner = derive_dev_account("budget-runtime-expiry").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(owner)).extra["netuid"]

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--below",
            "0.000001",  # impossible threshold — will never match
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "2s",
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


async def test_max_ru_exhaustion_emits_budget_exhausted(local_chain: LocalChain) -> None:
    """--max-ru exhaustion produces a schema-valid budget_exhausted payload."""
    # --max-ru 1 means the first successful RPC call exhausts the budget.
    # The watcher dispatches budget_exhausted on the tick that hits the limit.
    # Use a fresh subnet so this never races with any other worker.
    owner = derive_dev_account("budget-ru-exhaust").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(owner)).extra["netuid"]

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--below",
            "0.000001",  # impossible threshold so it doesn't match
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "10s",
            "--max-ru",
            "1",
        ],
        subprocess_timeout=15.0,
    )

    assert proc.returncode == 1, (
        f"expected exit 1 (budget_exhausted), got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "budget_exhausted"
    assert payload["reason"] == "max_ru_reached"
    _validate_schema(payload)
