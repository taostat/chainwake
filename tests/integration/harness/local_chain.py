"""Bittensor `LocalChain` test harness.

Wraps a `subtensor-localnet` container and exposes signed-extrinsic helpers
plus driver methods that map onto the six chainwake primitive shapes. Lives
under `tests/integration/harness/` so the CLAUDE.md "no keys" rule (which
applies to the chainwake package itself) is preserved — this is test
scaffolding, not production code.

Connectivity uses `async-substrate-interface` directly. Signing uses
`bittensor_wallet.Keypair.create_from_uri` against the standard Substrate
dev mnemonic (Alice/Bob/Charlie/Dave). No real funds, no real network.

"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

import httpx
from async_substrate_interface import AsyncSubstrateInterface
from bittensor_wallet import Keypair

from tests.integration.harness.protocol import DriverResult

DEFAULT_RPC_URL: Final[str] = "ws://127.0.0.1:9944"
DEFAULT_ETHEREUM_HTTP_URL: Final[str] = "http://127.0.0.1:8545"
COMPOSE_FILE: Final[Path] = (
    Path(__file__).resolve().parents[1] / "docker" / "docker-compose.subtensor.yml"
)
SS58_FORMAT: Final[int] = 42
TAO_TO_RAO: Final[int] = 1_000_000_000
READY_DEADLINE_SECONDS: Final[float] = 90.0
READY_POLL_SECONDS: Final[float] = 0.5
# Cross-worker lock path for Alice-signed extrinsics. /tmp is the right home
# here: the harness is test scaffolding, all xdist workers run on the same
# host as the localnet container, and the lock state is intentionally
# session-scoped (cleaned up on reboot). S108 is irrelevant for this use.
ALICE_LOCK_PATH: Final[Path] = Path("/tmp/chainwake-alice.lock")  # noqa: S108

# Per-process asyncio lock guarding any extrinsic signed by Alice. Pairs with
# the cross-process file lock at ``ALICE_LOCK_PATH`` so concurrent xdist
# workers (separate processes) and concurrent coroutines within a single
# worker both serialise through Alice's nonce. Held only across submit, which
# uses ``wait_for_inclusion=True`` — the lock releases after the chain has
# accepted the extrinsic, so the next signer can read the bumped nonce.
_alice_async_lock: asyncio.Lock = asyncio.Lock()


@asynccontextmanager
async def _alice_lock() -> AsyncIterator[None]:
    """Serialise Alice-signed extrinsics across processes and coroutines.

    Acquire the per-process asyncio lock, then a cross-process flock on
    ``ALICE_LOCK_PATH``. ``flock`` is a blocking syscall, so call it from a
    worker thread to keep the event loop responsive. Held until the wrapped
    extrinsic completes (typically ``wait_for_inclusion=True``) so the next
    holder reads the post-inclusion nonce on its first call to
    ``next_index``.
    """

    async with _alice_async_lock:
        fd = os.open(ALICE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# Canonical Substrate dev accounts must keep their well-known SURI suffixes.
# Alice in particular is the sudo key on subtensor-localnet, and the harness's
# sudo helpers (`bootstrap_disable_crv3`, `sudo_set_hyperparam`) all sign as
# `//Alice`. Sudo is global state that serialises through the chain regardless
# of worker, so worker-namespacing these would break sudo without any
# parallelism gain.
_CANONICAL_DEV_NAMES: Final[frozenset[str]] = frozenset(
    {"alice", "bob", "charlie", "dave", "eve", "ferdie"}
)


@dataclass(slots=True)
class DevAccount:
    """Pre-derived dev account from the standard Substrate dev phrase."""

    name: str
    keypair: Keypair

    @property
    def address(self) -> str:
        return self.keypair.ss58_address


def _docker_bin() -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker not on PATH; cannot run integration harness")
    return docker


def compose_up() -> None:
    docker = _docker_bin()
    subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
        check=True,
    )
    asyncio.run(_wait_for_local_chains_ready())


async def _wait_for_local_chains_ready() -> None:
    await asyncio.gather(
        _wait_for_rpc_ready(),
        _wait_for_anvil_ready(),
    )


async def _wait_for_rpc_ready(rpc_url: str = DEFAULT_RPC_URL) -> None:
    """Wait until the container serves a real Substrate JSON-RPC request."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_DEADLINE_SECONDS
    last_error: BaseException | None = None
    while loop.time() < deadline:
        substrate = AsyncSubstrateInterface(rpc_url, ss58_format=SS58_FORMAT)
        try:
            await asyncio.wait_for(substrate.initialize(), timeout=5)
            await asyncio.wait_for(substrate.get_block_number(None), timeout=5)
            return
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            last_error = exc
        finally:
            with contextlib.suppress(Exception):
                await substrate.close()
        await asyncio.sleep(READY_POLL_SECONDS)
    raise RuntimeError(
        f"subtensor-localnet RPC did not become ready at {rpc_url} "
        f"within {READY_DEADLINE_SECONDS:.0f}s: {last_error!r}"
    )


async def _wait_for_anvil_ready(http_url: str = DEFAULT_ETHEREUM_HTTP_URL) -> None:
    """Wait until the pinned Anvil container serves Ethereum JSON-RPC."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_DEADLINE_SECONDS
    last_error: BaseException | None = None
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_chainId",
        "params": [],
    }
    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(http_url, json=request)
                response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("result") == "0x1":
                return
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
        await asyncio.sleep(READY_POLL_SECONDS)
    raise RuntimeError(
        f"Anvil RPC did not become ready at {http_url} "
        f"within {READY_DEADLINE_SECONDS:.0f}s: {last_error!r}"
    )


def compose_down() -> None:
    docker = _docker_bin()
    subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_FILE), "down", "-v"],
        check=True,
    )


@cache
def derive_dev_account(name: str) -> DevAccount:
    """Return a dev account derived from a SURI suffix.

    Canonical Substrate dev accounts (alice/bob/charlie/dave/eve/ferdie)
    keep their well-known SURI (`//Alice` etc.) so sudo signing and
    pre-funded balances behave the same across workers. Every other
    label is prefixed with the active ``PYTEST_XDIST_WORKER`` id so
    parallel workers do not collide on a single derivation path.

    Cached: keypair derivation is deterministic and the same (worker,
    label) pair always resolves to the same keypair within a session.
    """

    lowered = name.lower()
    if lowered in _CANONICAL_DEV_NAMES:
        suri = f"//{lowered.capitalize()}"
        return DevAccount(name=lowered.capitalize(), keypair=Keypair.create_from_uri(suri))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    suri = f"//{worker}-{name}"
    return DevAccount(name=f"{worker}-{name}", keypair=Keypair.create_from_uri(suri))


def _treasury_keypair() -> Keypair:
    """Return the signing keypair for funder operations.

    Each xdist worker signs ``Balances.transfer_keep_alive`` from its own
    ``//treasury-{worker}`` SURI so concurrent ``fund_account`` calls don't
    share Alice's nonce. Outside xdist (``PYTEST_XDIST_WORKER`` unset), we
    sign as Alice — serial runs have a single nonce-holder, so there's no
    contention to avoid and Alice already has the genesis treasury. The
    master process pre-funds every worker treasury before any worker starts
    a test (see ``bootstrap_worker_treasuries``).
    """

    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        return derive_dev_account("alice").keypair
    return Keypair.create_from_uri(f"//treasury-{worker}")


def worker_treasury_address(worker: str) -> str:
    """Return the SS58 address of the ``//treasury-{worker}`` SURI."""

    return Keypair.create_from_uri(f"//treasury-{worker}").ss58_address


async def bootstrap_worker_treasuries(
    rpc_url: str,
    worker_ids: list[str],
    target_rao: int,
) -> None:
    """Top up every worker's treasury to ``target_rao`` from Alice.

    Runs in the xdist master process before worker forks, so all transfers
    serialise through Alice's single nonce — no concurrent submissions, no
    transaction-pool deduplication, no ``code=1012`` bans. Idempotent:
    treasuries already at or above ``target_rao`` are skipped, so re-using
    a localnet across sessions (``CHAINWAKE_REUSE_NODE=1``) doesn't drain
    Alice on every invocation. Each receipt is checked for ``is_success``
    so a silent ``FundsUnavailable`` doesn't leave a worker treasury empty.
    """

    alice = derive_dev_account("alice").keypair
    substrate = AsyncSubstrateInterface(rpc_url, ss58_format=SS58_FORMAT)
    await substrate.initialize()
    try:
        for worker in worker_ids:
            recipient = worker_treasury_address(worker)
            current = await _free_balance(substrate, recipient)
            if current >= target_rao:
                continue
            top_up = target_rao - current
            call = await substrate.compose_call(
                call_module="Balances",
                call_function="transfer_keep_alive",
                call_params={"dest": recipient, "value": top_up},
            )
            extrinsic = await substrate.create_signed_extrinsic(  # type: ignore[arg-type]
                call=call,
                keypair=alice,
            )
            receipt = await substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)
            is_success = await receipt.is_success
            if not is_success:
                err = await receipt.error_message
                raise RuntimeError(
                    f"failed to fund treasury for worker {worker} "
                    f"(top_up={top_up}, recipient={recipient}): {err!r}"
                )
    finally:
        await substrate.close()


async def bootstrap_disable_crv3(rpc_url: str, max_netuid: int = 256) -> None:
    """Disable commit-reveal v3 weights for every netuid in ``range(max_netuid)``.

    Subtensor's CRV3 module pulls randomness from drand.love public APIs on
    every weight-commit window. Under ``FAST_BLOCKS=True`` the localnet burns
    through drand's per-IP rate limit in a couple of hours, all endpoints
    start returning HTTP 429, retry workers pile up holding WASM executor
    instances, the WASM pool exhausts, and block production halts.

    Runs once on the xdist master before workers fork. Writes
    ``SubtensorModule.CommitRevealWeightsEnabled[netuid] = false`` for every
    netuid in a single ``Sudo(System.set_storage)`` extrinsic — one Alice
    transaction covers all current and future test-registered subnets, so
    individual ``register_subnet`` calls don't need to share Alice's nonce
    on a CRV3 write.
    """

    alice = derive_dev_account("alice").keypair
    substrate = AsyncSubstrateInterface(rpc_url, ss58_format=SS58_FORMAT)
    await substrate.initialize()
    try:
        items: list[tuple[str, str]] = []
        for netuid in range(max_netuid):
            storage_key = await substrate.create_storage_key(
                "SubtensorModule", "CommitRevealWeightsEnabled", [netuid]
            )
            key_hex = storage_key.to_hex()
            if key_hex is None:
                raise RuntimeError(f"create_storage_key returned no hex for netuid {netuid}")
            items.append((key_hex, "0x00"))
        inner = await substrate.compose_call(
            call_module="System",
            call_function="set_storage",
            call_params={"items": items},
        )
        sudo_call = await substrate.compose_call(
            call_module="Sudo",
            call_function="sudo",
            call_params={"call": inner.value},
        )
        extrinsic = await substrate.create_signed_extrinsic(  # type: ignore[arg-type]
            call=sudo_call,
            keypair=alice,
        )
        receipt = await substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)
        is_success = await receipt.is_success
        if not is_success:
            err = await receipt.error_message
            raise RuntimeError(f"failed to disable CRV3 across {max_netuid} netuids: {err!r}")
    finally:
        await substrate.close()


async def _free_balance(substrate: AsyncSubstrateInterface, address: str) -> int:
    """Read the free balance of ``address`` from System.Account."""

    info = await substrate.query("System", "Account", [address])
    if info is None:
        return 0
    data = info.value
    if not isinstance(data, dict):
        return 0
    return int(data["data"]["free"])


def tao_to_rao(tao: float) -> int:
    return int(tao * TAO_TO_RAO)


class LocalChain:
    """Driver-backed harness for subtensor-localnet."""

    rpc_url: str

    def __init__(self, rpc_url: str = DEFAULT_RPC_URL) -> None:
        self.rpc_url = rpc_url
        self._substrate: AsyncSubstrateInterface | None = None
        self._reuse = os.environ.get("CHAINWAKE_REUSE_NODE") == "1"
        self._owns_container = False

    @property
    def substrate(self) -> AsyncSubstrateInterface:
        if self._substrate is None:
            raise RuntimeError("LocalChain.start() not called yet")
        return self._substrate

    async def start(self) -> None:
        if not self._reuse:
            await asyncio.to_thread(compose_up)
            self._owns_container = True
        self._substrate = AsyncSubstrateInterface(self.rpc_url, ss58_format=SS58_FORMAT)
        await self.substrate.initialize()
        await self.wait_until_ready()

    async def wait_until_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + READY_DEADLINE_SECONDS
        last_err: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                head = await self.substrate.get_block_number(None)
                if head is not None and head > 0:
                    return
            except Exception as err:
                last_err = err
            await asyncio.sleep(READY_POLL_SECONDS)
        raise TimeoutError(
            f"subtensor-localnet did not produce blocks within {READY_DEADLINE_SECONDS}s; "
            f"last error: {last_err!r}"
        )

    async def stop(self) -> None:
        if self._substrate is not None:
            await self._substrate.close()
            self._substrate = None
        if self._owns_container and not self._reuse:
            await asyncio.to_thread(compose_down)
            self._owns_container = False

    async def head(self) -> int:
        head = await self.substrate.get_block_number(None)
        if head is None:
            raise RuntimeError("substrate returned None for head block number")
        return head

    async def submit_extrinsic_and_get_hash(
        self,
        call: object,
        signer: Keypair,
    ) -> tuple[str, int]:
        """Sign and submit an extrinsic; return (extrinsic_hash, block_number)."""

        extrinsic = await self.substrate.create_signed_extrinsic(  # type: ignore[arg-type]
            call=call,  # ty: ignore[invalid-argument-type]
            keypair=signer,
        )
        receipt = await self.substrate.submit_extrinsic(
            extrinsic,
            wait_for_inclusion=True,
        )
        block_hash = receipt.block_hash
        if block_hash is None:
            raise RuntimeError("submit_extrinsic returned no block_hash")
        block_number = await self.substrate.get_block_number(block_hash)
        if block_number is None:
            raise RuntimeError("could not resolve block number for receipt")
        extrinsic_hash = receipt.extrinsic_hash
        if extrinsic_hash is None:
            raise RuntimeError("submit_extrinsic returned no extrinsic_hash")
        return extrinsic_hash, block_number

    async def transfer(self, sender: Keypair, recipient: str, rao: int) -> DriverResult:
        call = await self.substrate.compose_call(
            call_module="Balances",
            call_function="transfer_keep_alive",
            call_params={"dest": recipient, "value": rao},
        )
        tx_hash, block = await self.submit_extrinsic_and_get_hash(call, sender)
        return DriverResult(block=block, extra={"tx_hash": tx_hash, "amount": rao})

    async def fund_account(self, recipient: str, rao: int) -> DriverResult:
        return await self.transfer(_treasury_keypair(), recipient, rao)

    async def total_networks(self) -> int:
        result = await self.substrate.query("SubtensorModule", "TotalNetworks")
        if result is None:
            return 0
        # ScaleType wraps a primitive; .value yields the underlying number.
        # Coerce via str() for ty's narrowing — int(str(n)) is safe for all
        # scalar substrate types used here.
        return int(str(getattr(result, "value", result)))

    async def _reset_lock_cost(self) -> None:
        """Reset NetworkLastLockCost to 1 TAO via Alice-signed sudo set_storage.

        Subtensor's ``register_network`` charges roughly ``2 *
        NetworkLastLockCost`` and the cost doubles every registration on
        FAST_BLOCKS. Resetting to 1 TAO before every registration keeps the
        per-test funding requirement constant. Wrapped in ``_alice_lock``
        so concurrent xdist workers serialise through Alice's nonce instead
        of racing the transaction pool.
        """

        alice = derive_dev_account("alice").keypair
        storage_key = await self.substrate.create_storage_key(
            "SubtensorModule", "NetworkLastLockCost", []
        )
        value_hex = "0x" + TAO_TO_RAO.to_bytes(16, "little").hex()
        inner = await self.substrate.compose_call(
            call_module="System",
            call_function="set_storage",
            call_params={"items": [(storage_key.to_hex(), value_hex)]},
        )
        sudo_call = await self.substrate.compose_call(
            call_module="Sudo",
            call_function="sudo",
            call_params={"call": inner.value},
        )
        async with _alice_lock():
            await self.submit_extrinsic_and_get_hash(sudo_call, alice)

    async def register_subnet(self, owner: Keypair) -> DriverResult:
        # Reset NetworkLastLockCost to 1 TAO so the next register_network
        # extrinsic costs exactly 1 TAO regardless of how many subnets the
        # localnet has produced this session. The reset is a sudo call —
        # wrapped in _alice_lock so concurrent xdist workers serialise
        # through Alice's nonce instead of racing to submit and getting
        # banned by the transaction pool.
        await self._reset_lock_cost()
        # Fund the owner with 10 TAO from the worker treasury — well above
        # the 1 TAO reset cost and well below treasury budget.
        await self.fund_account(owner.ss58_address, 10 * TAO_TO_RAO)
        before_int = await self.total_networks()
        call = await self.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="register_network",
            call_params={"hotkey": owner.ss58_address},
        )
        tx_hash, block = await self.submit_extrinsic_and_get_hash(call, owner)
        after_int = await self.total_networks()
        netuid = after_int - 1 if after_int > before_int else before_int
        return DriverResult(block=block, extra={"tx_hash": tx_hash, "netuid": netuid})

    async def sudo_set_hyperparam(
        self,
        netuid: int,
        name: str,
        value: object,
    ) -> DriverResult:
        alice = derive_dev_account("alice").keypair
        # AdminUtils call functions follow the pattern `sudo_set_<param>`.
        # The param name matches the suffix after `sudo_set_`.
        param_name = name.removeprefix("sudo_set_")
        inner = await self.substrate.compose_call(
            call_module="AdminUtils",
            call_function=name,
            call_params={"netuid": netuid, param_name: value},
        )
        sudo_call = await self.substrate.compose_call(
            call_module="Sudo",
            call_function="sudo",
            call_params={"call": inner.value},
        )
        async with _alice_lock():
            tx_hash, block = await self.submit_extrinsic_and_get_hash(sudo_call, alice)
        return DriverResult(block=block, extra={"tx_hash": tx_hash, "name": name})

    async def drive_price_move(self, netuid: int, pct: float) -> DriverResult:
        """Drive a measurable price move on a subnet pool via sudo set_storage.

        The provider computes price as `SubnetTAO[netuid] / SubnetAlphaIn[netuid]`.
        Setting a new value for `SubnetAlphaIn` produces a deterministic price
        change without requiring a real swap extrinsic (subnet-0 swap paths
        are gated by CRV3 / commit-reveal / TxRateLimit on localnet).

        Args:
            netuid: target subnet.
            pct: signed percent change to apply to price (positive raises,
                negative drops). E.g. `pct=10` raises price by 10%.
        """
        substrate = self.substrate
        tao_raw = await substrate.query("SubtensorModule", "SubnetTAO", [netuid])
        alpha_raw = await substrate.query("SubtensorModule", "SubnetAlphaIn", [netuid])
        tao = int(str(getattr(tao_raw, "value", tao_raw) or 0))
        alpha = int(str(getattr(alpha_raw, "value", alpha_raw) or 0))
        if tao <= 0 or alpha <= 0:
            raise RuntimeError(
                f"cannot drive price move on netuid {netuid}: pool is empty "
                f"(SubnetTAO={tao}, SubnetAlphaIn={alpha})"
            )
        # Constant-product math: price = tao/alpha. Setting new_alpha to
        # alpha * 100 / (100 + pct) leaves tao unchanged but produces a
        # measurable price shift of `pct` percent.
        new_alpha = max(1, round(alpha * 100.0 / (100.0 + pct)))
        block = await self._sudo_set_storage_u64(
            "SubtensorModule", "SubnetAlphaIn", [netuid], new_alpha
        )
        return DriverResult(
            block=block,
            extra={
                "netuid": netuid,
                "pct": pct,
                "tao": tao,
                "old_alpha": alpha,
                "new_alpha": new_alpha,
            },
        )

    async def _sudo_set_storage_u64(
        self,
        module: str,
        storage_fn: str,
        params: list[object],
        value: int,
    ) -> int:
        """Write a u64 value to a parameterised storage entry via sudo.

        Returns the block number at which the write landed.
        """
        alice = derive_dev_account("alice").keypair
        storage_key = await self.substrate.create_storage_key(module, storage_fn, params)
        value_hex = "0x" + value.to_bytes(8, "little").hex()
        inner = await self.substrate.compose_call(
            call_module="System",
            call_function="set_storage",
            call_params={"items": [(storage_key.to_hex(), value_hex)]},
        )
        sudo_call = await self.substrate.compose_call(
            call_module="Sudo",
            call_function="sudo",
            call_params={"call": inner.value},
        )
        async with _alice_lock():
            _, block = await self.submit_extrinsic_and_get_hash(sudo_call, alice)
        return block

    async def wait_for_block_advance(self, blocks: int) -> int:
        """Block until the chain head has advanced by `blocks` blocks.

        Returns the new head block number.
        """
        if blocks <= 0:
            return await self.head()
        start = await self.head()
        target = start + blocks
        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline:
            head = await self.head()
            if head >= target:
                return head
            await asyncio.sleep(0.25)
        raise TimeoutError(
            f"chain did not advance {blocks} blocks within 60s "
            f"(start={start}, last_head={await self.head()})"
        )

    async def drive_validator_silence(self, hotkey: str, epochs: int) -> DriverResult:
        """Wait for `epochs` blocks to elapse without setting weights.

        The liveness primitive watches `validator.{hotkey}.weights` (the
        `LastUpdate[1][uid]` storage value) and fires when it has not
        advanced for the configured `--silent-for` window. Driving silence
        is therefore "do nothing" — we just wait for blocks to elapse so
        the watcher can observe the silence window. The `epochs` parameter
        is interpreted as a block count for fast-block localnet testing
        (one tick per block), keeping tests well under a minute.
        """
        block = await self.wait_for_block_advance(epochs)
        return DriverResult(
            block=block,
            extra={"hotkey": hotkey, "blocks_advanced": epochs, "driver": "silence_block_wait"},
        )

    async def drive_subnet_registration(self) -> DriverResult:
        # Use the worker treasury keypair as owner so concurrent xdist workers
        # don't share Alice's nonce on this register_network signature.
        return await self.register_subnet(_treasury_keypair())

    async def drive_balance_change(self, address: str, delta: int) -> DriverResult:
        return await self.fund_account(address, delta)

    async def drive_hyperparam_change(self, netuid: int, name: str, value: object) -> DriverResult:
        return await self.sudo_set_hyperparam(netuid, name, value)

    async def drive_tx_to_finality(self) -> DriverResult:
        # Sign from the worker treasury (worker-namespaced) so concurrent
        # xdist workers don't share Alice's nonce on this transfer.
        bob = derive_dev_account("bob").keypair
        return await self.transfer(_treasury_keypair(), bob.ss58_address, tao_to_rao(0.001))


__all__ = [
    "DEFAULT_RPC_URL",
    "DevAccount",
    "LocalChain",
    "bootstrap_disable_crv3",
    "bootstrap_worker_treasuries",
    "derive_dev_account",
    "tao_to_rao",
    "worker_treasury_address",
]
