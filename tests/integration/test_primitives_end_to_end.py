"""End-to-end primitive integration tests.

One test per primitive shape, exercising the full path:

    CLI subprocess → runtime → provider → primitive → adapter → exit JSON

These are the integration gate that catches cross-stream wire-up bugs that
per-stream review can't see. The polymorphic-payload bug fixed in
`ba26153` (runtime hardcoded `ThresholdCondition`/`ObservedThreshold`,
crashed any non-threshold match) is the kind these tests are designed to
prevent.

Coverage:
  - threshold ✓
  - event     ✓
  - tx        ✓
  - state     ✓ via `bt account balance --on-change`
  - delta     ✓ via `bt subnet <netuid> price --rise-pct` driven by sudo storage write
  - liveness  ✓ via `bt validator weights --silent-for` with no weights set
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import jsonschema
import pytest

from tests.integration.harness.local_chain import (
    LocalChain,
    derive_dev_account,
    tao_to_rao,
)

pytestmark = pytest.mark.integration

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "output.json"


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text())


def _validate(payload: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(_schema()).validate(payload)


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


async def _run(
    args: list[str],
    *,
    subprocess_timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return cast(
        "subprocess.CompletedProcess[str]",
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "chainwake", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=subprocess_timeout,
            env=_env(),
        ),
    )


async def _popen(args: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "chainwake",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(),
    )


async def test_cli_subprocess_runner_keeps_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synchronous CLI execution must not starve the shared harness loop."""
    loop_advanced = threading.Event()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if not loop_advanced.wait(timeout=0.1):
            raise AssertionError("CLI runner blocked the asyncio event loop")
        return subprocess.CompletedProcess([], 0, "", "")

    async def advance_loop() -> None:
        await asyncio.sleep(0)
        loop_advanced.set()

    monkeypatch.setattr(subprocess, "run", fake_run)
    marker = asyncio.create_task(advance_loop())
    await _run(["--help"])
    await marker


async def test_threshold_match_via_cli(local_chain: LocalChain) -> None:
    """Threshold: register a fresh subnet, watch its price below an absurd ceiling.

    The freshly-registered subnet has a deterministic seed price well below
    `1000` TAO/alpha, so the threshold fires on the first read.
    """
    alice = derive_dev_account("streamX-threshold-owner").keypair
    await local_chain.fund_account(alice.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(alice)).extra["netuid"]

    proc = await _run(
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
            "15s",
        ],
        subprocess_timeout=25.0,
    )

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["primitive"] == "threshold"
    assert payload["condition"]["operator"] == "below"
    assert payload["condition"]["target"] == 1000.0
    assert payload["observed"]["path"].startswith("subnet.")
    _validate(payload)


async def test_event_match_via_cli(local_chain: LocalChain) -> None:
    """Event: watch `event --type subnet-registered`, register a subnet, expect a match.

    The watcher subscribes to the chain-wide event firehose, filters for
    `subnet-registered`, and exits on first match. We start the subprocess,
    give it a moment to attach the subscription, then register a fresh
    subnet via the harness.
    """
    alice = derive_dev_account("streamX-event-owner").keypair
    await local_chain.fund_account(alice.ss58_address, tao_to_rao(2000))

    proc = await _popen(
        [
            "bt",
            "event",
            "--type",
            "subnet-registered",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "20s",
        ]
    )

    try:
        # Allow the subscription to attach before triggering the event.
        await asyncio.sleep(2.0)
        await local_chain.register_subnet(alice)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n"
        f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
    )
    payload = json.loads(stdout.decode())
    assert payload["status"] == "matched"
    assert payload["watcher"]["primitive"] == "event"
    assert payload["condition"]["event_type"] == "subnet-registered"
    assert "raw_event" in payload["observed"]
    _validate(payload)


async def test_tx_match_via_cli(local_chain: LocalChain) -> None:
    """Tx: submit a transfer, watch its hash for `finalized` finality, expect a match.

    Fast-blocks localnet finalises within a few seconds. The watcher polls
    the provider's `get_block_finality` and emits an `ObservedTx` payload on
    the first finalised observation.
    """
    alice = derive_dev_account("alice").keypair
    bob = derive_dev_account("streamX-tx-recipient").keypair
    result = await local_chain.transfer(alice, bob.ss58_address, tao_to_rao(0.001))
    tx_hash = str(result.extra["tx_hash"])

    proc = await _run(
        [
            "bt",
            "tx",
            tx_hash,
            "--finality",
            "finalized",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "20s",
        ],
        subprocess_timeout=30.0,
    )

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["primitive"] == "tx"
    assert payload["condition"]["finality"] == "finalized"
    assert payload["observed"]["tx_hash"] == tx_hash
    assert payload["observed"]["finality"] == "finalized"
    _validate(payload)


async def test_state_match_via_cli(local_chain: LocalChain) -> None:
    """State: spawn an account balance --on-change watcher, fund the account, expect a match.

    The state primitive only fires on transitions of primitive scalar values
    (str / int / float / bool / None); balance is a float per the provider,
    which is the cleanest scalar state observable in the registry. The
    watcher captures the baseline on its first poll, then we fund the
    account; the next poll observes a new balance and emits a Match.
    """
    coldkey = derive_dev_account("streamG4-state-owner").keypair
    # Seed with a known starting balance so the change is unambiguous.
    await local_chain.fund_account(coldkey.ss58_address, tao_to_rao(1.0))

    proc = await _popen(
        [
            "bt",
            "account",
            coldkey.ss58_address,
            "balance",
            "--on-change",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "30s",
        ]
    )

    try:
        # Allow the watcher to capture its baseline before we mutate.
        await asyncio.sleep(2.0)
        await local_chain.drive_balance_change(coldkey.ss58_address, tao_to_rao(2.5))
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n"
        f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
    )
    payload = json.loads(stdout.decode())
    assert payload["status"] == "matched"
    assert payload["watcher"]["primitive"] == "state"
    assert payload["condition"]["operator"] == "on-change"
    observed = payload["observed"]
    assert observed["path"] == f"account.{coldkey.ss58_address}.balance"
    assert observed["value"] != observed["previous_value"]
    _validate(payload)


async def test_delta_match_via_cli(local_chain: LocalChain) -> None:
    """Delta: rise-pct on a fresh subnet's price, driven by a sudo pool write.

    Spawn a `--rise-pct 10 --window-blocks 50` watcher, let it capture a
    baseline, then issue a +25 % price move via `drive_price_move`. The
    delta primitive observes the new value within the same window and
    emits a Match.
    """
    owner = derive_dev_account("streamG4-delta-owner").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(owner)).extra["netuid"]

    proc = await _popen(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--rise-pct",
            "10",
            "--window-blocks",
            "50",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "30s",
        ]
    )

    try:
        # Let the delta primitive capture its first in-window tick.
        await asyncio.sleep(2.0)
        await local_chain.drive_price_move(int(str(netuid)), 25.0)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n"
        f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
    )
    payload = json.loads(stdout.decode())
    assert payload["status"] == "matched"
    assert payload["watcher"]["primitive"] == "delta"
    assert payload["condition"]["operator"] == "rise-pct"
    assert payload["condition"]["target"] == 10.0
    observed = payload["observed"]
    assert observed["path"] == f"subnet.{netuid}.pool.price"
    assert observed["delta_pct"] >= 10.0
    _validate(payload)


async def test_delta_baseline_match_via_composite_storage_subscription(
    local_chain: LocalChain,
) -> None:
    """An unwindowed price delta wakes when either subscribed reserve changes."""
    owner = derive_dev_account("price-storage-delta-owner").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(2000))
    netuid = (await local_chain.register_subnet(owner)).extra["netuid"]

    proc = await _popen(
        [
            "bt",
            "subnet",
            str(netuid),
            "price",
            "--rise-pct",
            "10",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "30s",
        ]
    )

    try:
        await asyncio.sleep(2.0)
        await local_chain.drive_price_move(int(str(netuid)), 25.0)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}\n"
        f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
    )
    assert "subscribed storage (2 keys)" in stderr.decode()
    payload = json.loads(stdout.decode())
    assert payload["status"] == "matched"
    assert payload["condition"]["window"] == {
        "unit": "ever",
        "value": "watcher-start",
    }
    observed = payload["observed"]
    assert observed["path"] == f"subnet.{netuid}.pool.price"
    assert observed["delta_pct"] >= 10.0
    _validate(payload)


async def test_liveness_match_via_cli(local_chain: LocalChain) -> None:
    """Liveness: silent-for fires when a registered hotkey never sets weights.

    The validator weights observable returns the block at which the hotkey
    last set weights. Registering a new subnet gives its owner a real neuron
    without requiring a weight update, so the liveness timer can advance
    without treating an unknown hotkey as legitimate zero-valued chain state.
    """
    owner = derive_dev_account("streamG4-liveness-owner").keypair
    await local_chain.fund_account(owner.ss58_address, tao_to_rao(1100.0))
    result = await local_chain.register_subnet(owner)
    netuid = int(str(result.extra["netuid"]))

    proc = await _run(
        [
            "bt",
            "validator",
            owner.ss58_address,
            "weights",
            "--netuid",
            str(netuid),
            "--silent-for",
            "3blocks",
            "--rpc-url",
            local_chain.rpc_url,
            "--max-runtime",
            "30s",
        ],
        subprocess_timeout=40.0,
    )

    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "matched"
    assert payload["watcher"]["primitive"] == "liveness"
    assert payload["condition"]["operator"] == "silent-for"
    assert payload["condition"]["duration"] == "3blocks"
    observed = payload["observed"]
    assert observed["path"] == f"validator.{owner.ss58_address}.weights"
    assert observed["elapsed"].endswith("blocks")
    _validate(payload)
