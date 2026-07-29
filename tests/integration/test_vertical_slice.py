"""CLI-to-provider vertical slice integration test.

Spawns `chainwake` as a subprocess, drives it against the localnet, asserts
the exit JSON validates against `schemas/output.json`, and asserts the
exit code matches the documented contract.
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


async def test_vertical_slice_matches_when_price_below(
    local_chain: LocalChain,
) -> None:
    head = await local_chain.head()
    assert head > 0

    # Register a fresh subnet so this test does not race with any other
    # worker exercising subnet 1.  The freshly-registered pool price is
    # well below 1000 TAO/alpha, mirroring the threshold pattern used by
    # ``test_primitives_end_to_end::test_threshold_match_via_cli``.
    owner = derive_dev_account("vertical-slice-below").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(owner)).extra["netuid"]

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--below",
            "1000",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "10s",
        ]
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
    assert payload["condition"]["target"] == 1000.0
    assert payload["observed"]["path"] == f"subnet.{netuid}.pool.price"
    assert payload["observed"]["value"] < 1000.0
    assert payload["observed"]["block"] > 0

    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


async def test_vertical_slice_times_out_when_price_never_drops(
    local_chain: LocalChain,
) -> None:
    # Register a fresh subnet so the impossible threshold cannot race with
    # any other worker mutating subnet 1.  ``--below 0.000001`` is below
    # any realistic freshly-registered pool price.
    owner = derive_dev_account("vertical-slice-timeout").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(owner)).extra["netuid"]

    proc = await _run_cli(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--below",
            "0.000001",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "3s",
        ],
        subprocess_timeout=15.0,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}\nstdout: {proc.stdout}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "timeout"
    assert payload["reason"] == "max_runtime_reached"
    assert payload["observed"] is None

    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)
