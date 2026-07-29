"""Project-root conftest.

Hosts the xdist master `pytest_configure` hook for the integration suite so
the master loads it even when test collection is delegated entirely to
workers (which leaves `tests/integration/conftest.py` unloaded on master).

The hook bootstraps per-worker treasury keypairs from Alice before any
worker forks, so `LocalChain.fund_account` calls in workers don't all
share Alice's nonce.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from tests.integration.harness.local_chain import (
    DEFAULT_RPC_URL,
    bootstrap_disable_crv3,
    bootstrap_worker_treasuries,
    compose_down,
    compose_up,
    tao_to_rao,
)

_log = logging.getLogger(__name__)

# Bag size topped up into each worker treasury, in TAO. Each test funds
# accounts with at most ~2,000 TAO and there are ~44 tests, so 10,000 TAO
# per worker covers any plausible per-worker cost (load-balanced across
# N workers, each worker sees ~44/N tests). Alice starts with 1,000,000
# TAO on subtensor-localnet but also funds owners during sudo helpers,
# so this stays small to leave her plenty of headroom even at high -n.
# Bootstrapping is idempotent (top-up to target, skip when already at or
# above), so re-using a localnet across `CHAINWAKE_REUSE_NODE=1` sessions
# doesn't repeatedly drain Alice.
_WORKER_TREASURY_TAO: int = 10_000

# State for `pytest_unconfigure` teardown. Set when the master process boots
# the container itself (not under `CHAINWAKE_REUSE_NODE=1`) and resets
# `CHAINWAKE_REUSE_NODE` so each worker attaches without trying to
# compose-up again. Wrapped in a dict to avoid the `global` statement.
_master_state: dict[str, bool] = {"owns_container": False, "set_reuse": False}


def _xdist_worker_count(config: pytest.Config) -> int:
    """Return the configured xdist worker count, or 0 if xdist is disabled."""

    numprocesses = getattr(config.option, "numprocesses", None)
    if numprocesses is None:
        return 0
    if isinstance(numprocesses, int):
        return numprocesses
    # `-n auto` / `-n logical` are resolved to int by xdist before the master
    # forks workers, but defensively coerce in case xdist hands us a string.
    try:
        return int(numprocesses)
    except (TypeError, ValueError):
        return 0


def _is_xdist_master(config: pytest.Config) -> bool:
    """True only on the xdist controller (not a worker, not a serial run)."""

    return getattr(config, "workerinput", None) is None


def _integration_requested(config: pytest.Config) -> bool:
    """Whether this pytest invocation includes the Docker integration suite."""

    markexpr = str(getattr(config.option, "markexpr", "") or "")
    if markexpr:
        return "integration" in markexpr and "not integration" not in markexpr
    args = [str(arg) for arg in getattr(config, "args", ())]
    if args:
        return any("tests/integration" in arg for arg in args)
    # With no explicit marker or test path pytest collects the configured
    # testpaths, which includes integration tests.
    return True


def pytest_configure(config: pytest.Config) -> None:
    """Bootstrap shared chain state on the xdist master before workers fork.

    Runs only when xdist is active with >1 worker. Two pieces of session-level
    setup happen here, both signing as Alice in this single process so every
    extrinsic serialises through Alice's nonce — no concurrency, no race, no
    ``code=1012`` bans:

    1. Disable commit-reveal v3 across all 256 netuids in a single
       ``Sudo(System.set_storage)`` extrinsic. Covers every netuid that test
       ``register_subnet`` calls will ever produce, so individual workers no
       longer need a per-subnet sudo write.
    2. Top up each ``//treasury-gw{i}`` SURI to ``_WORKER_TREASURY_TAO`` with
       ``wait_for_inclusion=True``. Workers then sign their own
       ``fund_account`` calls with their treasury keypair.

    Lives in the project-root conftest because xdist's master process may
    skip collection entirely and never loads `tests/integration/conftest.py`,
    so a hook there would never fire on master.
    """

    if not _is_xdist_master(config):
        return
    if not _integration_requested(config):
        return
    workers = _xdist_worker_count(config)
    if workers <= 1:
        return
    reuse = os.environ.get("CHAINWAKE_REUSE_NODE") == "1"
    if not reuse:
        compose_up()
        _master_state["owns_container"] = True
    worker_ids = [f"gw{i}" for i in range(workers)]
    rao = tao_to_rao(float(_WORKER_TREASURY_TAO))
    _log.info(
        "bootstrapping CRV3-disable + %d worker treasuries from Alice (target %d TAO each)",
        workers,
        _WORKER_TREASURY_TAO,
    )
    asyncio.run(_bootstrap(worker_ids, rao))
    if not reuse:
        # Workers must attach to the master-owned container instead of
        # racing each other on `docker compose up -d`.
        os.environ["CHAINWAKE_REUSE_NODE"] = "1"
        _master_state["set_reuse"] = True


async def _bootstrap(worker_ids: list[str], target_rao: int) -> None:
    """Run session-level chain bootstrap steps in order.

    Single async entrypoint so both bootstrap helpers share the master
    process's event loop and Alice's nonce serialises through them.
    """

    await bootstrap_disable_crv3(DEFAULT_RPC_URL)
    await bootstrap_worker_treasuries(DEFAULT_RPC_URL, worker_ids, target_rao)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Tear down the master-owned container after the xdist run completes."""

    if not _is_xdist_master(config):
        return
    if _master_state["set_reuse"]:
        os.environ.pop("CHAINWAKE_REUSE_NODE", None)
        _master_state["set_reuse"] = False
    if _master_state["owns_container"]:
        compose_down()
        _master_state["owns_container"] = False
