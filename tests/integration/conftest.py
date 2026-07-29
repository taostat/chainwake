"""Session-scoped fixtures for chainwake integration tests.

Per-worker treasury bootstrap lives in the project-root ``conftest.py`` so
the xdist master loads the hook even when collection is delegated entirely
to workers.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tests.integration.harness.local_chain import (
    DEFAULT_RPC_URL,
    LocalChain,
    bootstrap_disable_crv3,
)

_log = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session")
async def local_chain() -> AsyncIterator[LocalChain]:
    chain = LocalChain()
    await chain.start()
    # In serial mode (no xdist worker), the project-root pytest_configure
    # hook short-circuits and never disables CRV3 — do it here instead. In
    # xdist mode, the master process already wrote CommitRevealWeightsEnabled
    # for every netuid; workers skip this step. The write is idempotent so
    # repeated CHAINWAKE_REUSE_NODE=1 sessions do not pile up extrinsics.
    if os.environ.get("PYTEST_XDIST_WORKER") is None:
        await bootstrap_disable_crv3(DEFAULT_RPC_URL)
    try:
        yield chain
    finally:
        await chain.stop()


@pytest.fixture(autouse=True)
def _log_test_boundaries(request: pytest.FixtureRequest) -> Iterator[None]:
    """Emit enter/exit log markers for every test.

    With `--log-cli-level=INFO`, each test prints its `>>>` and `<<<`
    boundaries. Tests that hang (or hang during teardown) show their `>>>`
    but never their `<<<`, making stuck tests obvious in CI logs.
    """
    _log.info(">>> %s", request.node.nodeid)
    yield
    _log.info("<<< %s", request.node.nodeid)
